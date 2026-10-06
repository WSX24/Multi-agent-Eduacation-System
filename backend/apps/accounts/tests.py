import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from .models import EmailVerificationCode


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    DEFAULT_FROM_EMAIL='noreply@example.com',
)
class RegistrationEmailCodeTests(APITestCase):
    url = '/api/accounts/email-codes/'

    def setUp(self):
        cache.clear()

    def send_code(self, email='student@example.com'):
        return self.client.post(self.url, {
            'email': email,
            'purpose': 'register',
        }, format='json')

    def test_sends_code_and_stores_only_hash(self):
        response = self.send_code(' Student@Example.com ')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['code'], 200)
        self.assertEqual(response.data['data'], {
            'code_valid_for': 600,
            'retry_after': 60,
        })
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['student@example.com'])

        code = re.search(r'\b\d{6}\b', mail.outbox[0].body).group()
        record = EmailVerificationCode.objects.get(email='student@example.com')
        self.assertNotEqual(record.code_hash, code)
        self.assertTrue(check_password(code, record.code_hash))
        self.assertEqual((record.expires_at - record.sent_at).total_seconds(), 600)

    def test_registered_email_still_receives_code(self):
        get_user_model().objects.create_user(email='student@example.com', password='Example123!')

        response = self.send_code()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

    def test_resend_within_a_minute_is_rate_limited(self):
        self.send_code()

        response = self.send_code()

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.data['code'], 429)
        self.assertGreater(response.data['data']['retry_after'], 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_invalid_email_uses_common_error_shape(self):
        response = self.send_code('invalid-email')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 400)
        self.assertIn('email', response.data['errors'])
        self.assertEqual(len(mail.outbox), 0)

    @patch('apps.accounts.services.send_mail', side_effect=OSError('Console output failed'))
    def test_mail_failure_rolls_back_code(self, _send_mail):
        response = self.send_code()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data['code'], 503)
        self.assertFalse(EmailVerificationCode.objects.exists())
