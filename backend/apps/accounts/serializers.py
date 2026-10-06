from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError

from .models import EmailVerificationCode


class RegistrationEmailCodeSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    purpose = serializers.ChoiceField(choices=EmailVerificationCode.Purpose.choices)

    def validate_email(self, value):
        return value.strip().casefold()


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    password_confirm = serializers.CharField(write_only=True, trim_whitespace=False)
    verification_code = serializers.RegexField(r'^\d{6}$')
    accepted_terms = serializers.BooleanField()

    def validate_email(self, value):
        return value.strip().casefold()

    def validate(self, attrs):
        errors = {}
        if attrs['password'] != attrs['password_confirm']:
            errors['password_confirm'] = ['两次输入的密码不一致。']
        if len(attrs['password']) < 8 or not any(c.isdigit() for c in attrs['password']):
            errors['password'] = ['密码至少 8 位，并且需要包含数字。']
        else:
            try:
                validate_password(attrs['password'], user=get_user_model()(email=attrs['email']))
            except DjangoValidationError as exc:
                errors['password'] = list(exc.messages)
        if not attrs['accepted_terms']:
            errors['accepted_terms'] = ['请先同意服务条款与隐私政策。']
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(trim_whitespace=False)

    def validate_email(self, value):
        return value.strip().casefold()


class RefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField(trim_whitespace=False)
