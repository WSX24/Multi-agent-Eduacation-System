from rest_framework import serializers


class FieldErrorsSerializer(serializers.DictField):
    child = serializers.ListField(child=serializers.CharField())


class ErrorResponseSerializer(serializers.Serializer):
    code = serializers.IntegerField()
    message = serializers.CharField()
    data = serializers.JSONField(allow_null=True)
    errors = FieldErrorsSerializer(allow_null=True)


class UserDataSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    email = serializers.EmailField()


class TokenDataSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField()
    user = UserDataSerializer()


class AccessDataSerializer(serializers.Serializer):
    access = serializers.CharField()


class EmailCodeDataSerializer(serializers.Serializer):
    code_valid_for = serializers.IntegerField()
    retry_after = serializers.IntegerField()


class EmailCodeResponseSerializer(serializers.Serializer):
    code = serializers.IntegerField()
    message = serializers.CharField()
    data = EmailCodeDataSerializer()
    errors = serializers.JSONField(allow_null=True)


class AuthResponseSerializer(serializers.Serializer):
    code = serializers.IntegerField()
    message = serializers.CharField()
    data = TokenDataSerializer()
    errors = serializers.JSONField(allow_null=True)


class AccessResponseSerializer(serializers.Serializer):
    code = serializers.IntegerField()
    message = serializers.CharField()
    data = AccessDataSerializer()
    errors = serializers.JSONField(allow_null=True)


class UserResponseSerializer(serializers.Serializer):
    code = serializers.IntegerField()
    message = serializers.CharField()
    data = UserDataSerializer()
    errors = serializers.JSONField(allow_null=True)


class EmptyResponseSerializer(serializers.Serializer):
    code = serializers.IntegerField()
    message = serializers.CharField()
    data = serializers.JSONField(allow_null=True)
    errors = serializers.JSONField(allow_null=True)
