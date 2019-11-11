"""passbook app_gw models"""
import string
from random import SystemRandom

from django.db import models
from django.utils.translation import gettext as _
from oidc_provider.models import Client

from passbook import __version__
from passbook.core.models import Provider


class ApplicationGatewayProvider(Provider):
    """This provider uses oauth2_proxy with the OIDC Provider."""

    name = models.TextField()
    host = models.TextField()

    client = models.ForeignKey(Client, on_delete=models.CASCADE)

    form = 'passbook.providers.app_gw.forms.ApplicationGatewayProviderForm'

    def html_setup_urls(self, request):
        """return template and context modal with URLs for authorize, token, openid-config, etc"""
        cookie_secret = ''.join(SystemRandom().choice(
            string.ascii_uppercase + string.digits) for _ in range(50))
        return "app_gw/setup_modal.html", {
            'provider': self,
            'cookie_secret': cookie_secret,
            'version': __version__
        }

    def __str__(self):
        return f"Application Gateway {self.name}"

    class Meta:

        verbose_name = _('Application Gateway Provider')
        verbose_name_plural = _('Application Gateway Providers')
