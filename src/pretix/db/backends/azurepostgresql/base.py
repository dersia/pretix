import psycopg2 as Database
import logging
from contextlib import contextmanager
import django.db.backends.postgresql.base as postgresqlBackend
from django.utils.asyncio import async_unsafe
from django.utils.functional import cached_property
from django.utils.safestring import SafeString
from django.utils.version import get_version_tuple
from django.db.backends.postgresql.psycopg_any import IsolationLevel, is_psycopg3  # NOQA isort:skip
from django.db.utils import OperationalError, ProgrammingError
from azure.identity import DefaultAzureCredential
from azure.core.credentials import AccessToken
from azure.keyvault.secrets import SecretClient
from datetime import datetime, timedelta
from django.core.exceptions import ImproperlyConfigured
from django.conf import settings


def psycopg_version():
    version = Database.__version__.split(" ", 1)[0]
    return get_version_tuple(version)


if psycopg_version() < (2, 8, 4):
    raise ImproperlyConfigured(
        f"psycopg2 version 2.8.4 or newer is required; you have {Database.__version__}"
    )
if (3,) <= psycopg_version() < (3, 1, 8):
    raise ImproperlyConfigured(
        f"psycopg version 3.1.8 or newer is required; you have {Database.__version__}"
    )


from django.db.backends.postgresql.psycopg_any import IsolationLevel, is_psycopg3  # NOQA isort:skip

if is_psycopg3:
    from psycopg2 import adapters

    from django.db.backends.postgresql.psycopg_any import get_adapters_template, register_tzloader

    TIMESTAMPTZ_OID = adapters.types["timestamptz"].oid

else:
    import psycopg2.extensions
    import psycopg2.extras

    psycopg2.extensions.register_adapter(SafeString, psycopg2.extensions.QuotedString)
    psycopg2.extras.register_uuid()

    # Register support for inet[] manually so we don't have to handle the Inet()
    # object on load all the time.
    INETARRAY_OID = 1041
    INETARRAY = psycopg2.extensions.new_array_type(
        (INETARRAY_OID,),
        "INETARRAY",
        psycopg2.extensions.UNICODE,
    )
    psycopg2.extensions.register_type(INETARRAY)

class DatabaseWrapper(postgresqlBackend.DatabaseWrapper):

    azure_token: AccessToken | str = None
    azure_credential: DefaultAzureCredential | str = None
    debug_fix = False

    def get_connection_params(self):
        settings_dict = self.settings_dict

        # None may be used to connect to the default 'postgres' db
        if settings_dict["NAME"] == "" and not settings_dict.get("OPTIONS", {}).get(
            "service"
        ):
            raise ImproperlyConfigured(
                "settings.DATABASES is improperly configured. "
                "Please supply the NAME or OPTIONS['service'] value."
            )
        if len(settings_dict["NAME"] or "") > self.ops.max_name_length():
            raise ImproperlyConfigured(
                "The database name '%s' (%d characters) is longer than "
                "PostgreSQL's limit of %d characters. Supply a shorter NAME "
                "in settings.DATABASES."
                % (
                    settings_dict["NAME"],
                    len(settings_dict["NAME"]),
                    self.ops.max_name_length(),
                )
            )
        if settings_dict["NAME"]:
            conn_params = {
                "dbname": settings_dict["NAME"],
                **settings_dict["OPTIONS"],
            }
        elif settings_dict["NAME"] is None:
            # Connect to the default 'postgres' db.
            settings_dict.get("OPTIONS", {}).pop("service", None)
            conn_params = {"dbname": "postgres", **settings_dict["OPTIONS"]}
        else:
            conn_params = {**settings_dict["OPTIONS"]}

        if 'sslmode' not in settings_dict["OPTIONS"]:
            conn_params["sslmode"] = "require"

        conn_params["client_encoding"] = "UTF8"

        conn_params.pop("assume_role", None)
        conn_params.pop("isolation_level", None)
        server_side_binding = conn_params.pop("server_side_binding", None)
        conn_params.setdefault(
            "cursor_factory",
            postgresqlBackend.ServerBindingCursor
            if is_psycopg3 and server_side_binding is True
            else postgresqlBackend.Cursor,
        )
        if settings_dict["USER"]:
            conn_params["user"] = settings_dict["USER"]
        if settings_dict["PASSWORD"]:
            conn_params["password"] = settings_dict["PASSWORD"]
        if settings_dict["HOST"]:
            conn_params["host"] = settings_dict["HOST"]
        if settings_dict["PORT"]:
            conn_params["port"] = settings_dict["PORT"]
        if is_psycopg3:
            conn_params["context"] = get_adapters_template(
                settings.USE_TZ, self.timezone
            )
            # Disable prepared statements by default to keep connection poolers
            # working. Can be reenabled via OPTIONS in the settings dict.
            conn_params["prepare_threshold"] = conn_params.pop(
                "prepare_threshold", None
            )
        if settings_dict['AZURE']:
            managed_identity = settings_dict['AZURE'].get('managed_identity', None)
            tenant_id = settings_dict['AZURE'].get('tenant_id', None)
            if self.azure_credential is None and managed_identity is not None and tenant_id is not None:
                self.azure_credential = DefaultAzureCredential(
                    managed_identity_client_id=managed_identity,
                    visual_studio_code_tenant_id=tenant_id,
                    shared_cache_tenant_id=tenant_id,
                )
            self.__refreshCredentials(conn_params, tenant_id)

        return conn_params

    @async_unsafe
    def get_new_connection(self, conn_params):
        # self.isolation_level must be set:
        # - after connecting to the database in order to obtain the database's
        #   default when no value is explicitly specified in options.
        # - before calling _set_autocommit() because if autocommit is on, that
        #   will set connection.isolation_level to ISOLATION_LEVEL_AUTOCOMMIT.
        options = self.settings_dict["OPTIONS"]
        set_isolation_level = False
        try:
            isolation_level_value = options["isolation_level"]
        except KeyError:
            self.isolation_level = IsolationLevel.READ_COMMITTED
        else:
            # Set the isolation level to the value from OPTIONS.
            try:
                self.isolation_level = IsolationLevel(isolation_level_value)
                set_isolation_level = True
            except ValueError:
                raise ImproperlyConfigured(
                    f"Invalid transaction isolation level {isolation_level_value} "
                    f"specified. Use one of the psycopg.IsolationLevel values."
                )
        connection = self.Database.connect(**conn_params)
        if set_isolation_level:
            connection.isolation_level = self.isolation_level
        if not is_psycopg3:
            # Register dummy loads() to avoid a round trip from psycopg2's
            # decode to json.dumps() to json.loads(), when using a custom
            # decoder in JSONField.
            psycopg2.extras.register_default_jsonb(
                conn_or_curs=connection, loads=lambda x: x
            )
        return connection

    @async_unsafe
    def __refreshCredentials(self, conn_params, tenant_id):
        """Refresh the database credentials if necessary."""
        if self.debug_fix:
            logging.info("Debug fix is enabled.")
            conn_params['user'] = "sia@sia-consulting.eu"
        if self.azure_credential is None:
            return
        if self.azure_token is not None and self.azure_token == "disabled":
            return
        if isinstance(self.azure_token, AccessToken) and self.azure_token.token != "" and self.azure_token.expires_on > (datetime.now() - timedelta(minutes=3)).timestamp():
            conn_params['password'] = self.azure_token.token
            return
        
        logging.info("Refreshing Azure credentials")

        if 'password' in conn_params and conn_params['password'] != "":
            self.azure_token = "disabled"
            return
        
        try:
            logging.info("Acquiring new Token for Managed Identity")
            self.azure_token = self.azure_credential.get_token("https://ossrdbms-aad.database.windows.net", tenant_id=tenant_id)
            if self.azure_token.token != "":
                conn_params['password'] = self.azure_token.token
        except Exception as e:
            logging.error("Error loading managed identity")
            logging.error(e)
            raise e


    @async_unsafe
    def ensure_connection(self):
        """Guarantee that a connection to the database is established."""
        if self.connection is None:
            with self.wrap_database_errors:
                try:
                    self.connect()
                except psycopg2.OperationalError as e:
                    logging.error('Connection to database failed: ', e)
                    logging.error('Trying to refresh credentials')
                    self.connect()
                
