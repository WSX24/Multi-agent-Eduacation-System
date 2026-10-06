import math
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import EmailVerificationCode


CODE_VALID_FOR = 600
RESEND_DELAY = 60
MAX_CODE_ATTEMPTS = 5


class EmailCodeCooldown(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


class EmailDeliveryFailed(Exception):
    pass


def send_registration_code(email):
    now = timezone.now()
    code = f'{secrets.randbelow(1_000_000):06d}'
    expires_at = now + timedelta(seconds=CODE_VALID_FOR)

    # Roll back the code if writing the development email fails.
    with transaction.atomic():
        record, created = EmailVerificationCode.objects.select_for_update().get_or_create(
            email=email,
            purpose=EmailVerificationCode.Purpose.REGISTER,
            defaults={
                'code_hash': make_password(code),
                'sent_at': now,
                'expires_at': expires_at,
            },
        )
        if not created:
            remaining = RESEND_DELAY - (now - record.sent_at).total_seconds()
            if remaining > 0:
                raise EmailCodeCooldown(math.ceil(remaining))
            record.code_hash = make_password(code)
            record.sent_at = now
            record.expires_at = expires_at
            record.attempt_count = 0
            record.used_at = None
            record.save(update_fields=[
                'code_hash', 'sent_at', 'expires_at', 'attempt_count', 'used_at',
            ])

        try:
            delivered = send_mail(
                subject='智塾注册验证码',
                message=f'您的注册验证码是 {code}，10 分钟内有效。请勿将验证码告诉他人。',
                from_email=None,
                recipient_list=[email],
                fail_silently=False,
            )
        except OSError as exc:
            raise EmailDeliveryFailed('Could not write verification email to the console.') from exc

        if delivered != 1:
            raise EmailDeliveryFailed('Console email backend did not print the message.')

    return {'code_valid_for': CODE_VALID_FOR, 'retry_after': RESEND_DELAY}


class EmailAlreadyRegistered(Exception):
    pass


class InvalidRegistrationCode(Exception):
    pass


def register_user(email, password, verification_code):
    user_model = get_user_model()
    result = None
    error = None

    try:
        with transaction.atomic():
            if user_model.objects.filter(email__iexact=email).exists():
                raise EmailAlreadyRegistered()

            record = EmailVerificationCode.objects.select_for_update().filter(
                email=email,
                purpose=EmailVerificationCode.Purpose.REGISTER,
            ).first()
            now = timezone.now()
            if record is None or record.used_at or record.expires_at <= now or record.attempt_count >= MAX_CODE_ATTEMPTS:
                error = InvalidRegistrationCode()
            elif not check_password(verification_code, record.code_hash):
                record.attempt_count += 1
                record.save(update_fields=['attempt_count'])
                error = InvalidRegistrationCode()
            else:
                result = user_model.objects.create_user(
                    email=email,
                    password=password,
                    terms_accepted_at=now,
                )
                record.used_at = now
                record.save(update_fields=['used_at'])
    except IntegrityError as exc:
        if user_model.objects.filter(email__iexact=email).exists():
            raise EmailAlreadyRegistered() from exc
        raise

    if error:
        raise error
    return result
