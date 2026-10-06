import logging

from django.contrib.auth import authenticate, get_user_model
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from drf_spectacular.utils import extend_schema

from .schema import (
    AccessResponseSerializer, AuthResponseSerializer, EmailCodeResponseSerializer,
    EmptyResponseSerializer, ErrorResponseSerializer, UserResponseSerializer,
)
from .serializers import (
    LoginSerializer, RefreshSerializer, RegisterSerializer,
    RegistrationEmailCodeSerializer,
)
from .services import (
    EmailAlreadyRegistered, EmailCodeCooldown, EmailDeliveryFailed,
    InvalidRegistrationCode, register_user, send_registration_code,
)


logger = logging.getLogger(__name__)


def api_response(code, message, data=None, errors=None):
    return Response(
        {'code': code, 'message': message, 'data': data, 'errors': errors},
        status=code,
    )


def user_data(user):
    return {'id': user.pk, 'email': user.email}


def token_data(user):
    refresh = RefreshToken.for_user(user)
    return {
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'user': user_data(user),
    }


class RegistrationEmailCodeView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'registration_email_code'

    @extend_schema(
        tags=['账号'],
        summary='发送注册验证码',
        request=RegistrationEmailCodeSerializer,
        responses={
            200: EmailCodeResponseSerializer,
            400: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request):
        serializer = RegistrationEmailCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            result = send_registration_code(serializer.validated_data['email'])
        except EmailCodeCooldown as exc:
            return Response({
                'code': 429,
                'message': '请求过于频繁，请稍后再试。',
                'data': {'retry_after': exc.retry_after},
                'errors': None,
            }, status=status.HTTP_429_TOO_MANY_REQUESTS)
        except EmailDeliveryFailed as exc:
            logger.warning('Registration verification email could not be sent: %s', exc)
            return Response({
                'code': 503,
                'message': '验证码暂时发送失败，请稍后重试。',
                'data': None,
                'errors': None,
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({
            'code': 200,
            'message': '验证码已发送，请查收邮件。',
            'data': result,
            'errors': None,
        })


class RegisterView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        tags=['账号'],
        summary='注册并自动登录',
        request=RegisterSerializer,
        responses={
            201: AuthResponseSerializer,
            400: ErrorResponseSerializer,
            409: ErrorResponseSerializer,
        },
    )
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data

        try:
            user = register_user(
                values['email'], values['password'], values['verification_code'],
            )
        except EmailAlreadyRegistered:
            return api_response(409, '该邮箱已注册，请直接登录。')
        except InvalidRegistrationCode:
            return api_response(
                400, '请求参数不合法。',
                errors={'verification_code': ['验证码错误或已过期，请重新获取。']},
            )

        return api_response(201, '注册成功。', token_data(user))


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        tags=['账号'],
        summary='邮箱密码登录',
        request=LoginSerializer,
        responses={
            200: AuthResponseSerializer,
            400: ErrorResponseSerializer,
            401: ErrorResponseSerializer,
        },
    )
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        user = authenticate(request, email=values['email'], password=values['password'])
        if user is None or not user.is_active:
            return api_response(401, '邮箱或密码错误。')
        return api_response(200, '登录成功。', token_data(user))


class TokenRefreshView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        tags=['账号'],
        summary='刷新访问令牌',
        request=RefreshSerializer,
        responses={
            200: AccessResponseSerializer,
            401: ErrorResponseSerializer,
        },
    )
    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(401, '登录状态已过期，请重新登录。')
        try:
            refresh = RefreshToken(serializer.validated_data['refresh'])
            user_id = refresh['user_id']
            if not get_user_model().objects.filter(pk=user_id, is_active=True).exists():
                return api_response(401, '登录状态已过期，请重新登录。')
            access = str(refresh.access_token)
        except (TokenError, KeyError, ValueError, TypeError):
            return api_response(401, '登录状态已过期，请重新登录。')
        return api_response(200, '访问令牌已刷新。', {'access': access})


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=['账号'],
        summary='获取当前用户',
        responses={
            200: UserResponseSerializer,
            401: ErrorResponseSerializer,
        },
    )
    def get(self, request):
        return api_response(200, '获取用户信息成功。', user_data(request.user))


class LogoutView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        tags=['账号'],
        summary='退出登录',
        request=RefreshSerializer,
        responses={
            200: EmptyResponseSerializer,
            401: ErrorResponseSerializer,
        },
    )
    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(401, '登录状态已过期，请重新登录。')
        try:
            RefreshToken(serializer.validated_data['refresh']).blacklist()
        except (TokenError, ValueError, TypeError):
            return api_response(401, '登录状态已过期，请重新登录。')
        return api_response(200, '已退出登录。')
