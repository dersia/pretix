import configparser
from django.db import connections
from django.db.utils import OperationalError, ProgrammingError
from django.conf import settings
from pretix.settings import get_db_password
from .helpers.config import EnvOrParserConfig

_config = configparser.RawConfigParser()

config = EnvOrParserConfig(_config)

class DbAuthenticationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Code to be executed for each request before
        # the view (and later middleware) are called.

        try:
            print('checking database')
            connections['default'].ensure_connection()
        except OperationalError as e:
            print('connection failed reloading')
            db_backend = config.get("database", "backend")
            newDbPw = get_db_password(db_backend)
            print('got new password, setting')
            databases = settings.DATABASES
            databases['default']['PASSWORD'] = newDbPw
            connections['default'].PASSWORD = newDbPw
            connections['default'].ensure_connection()
            print('updated DB connection')

        response = self.get_response(request)

        # Code to be executed for each request/response after
        # the view is called.

        return response