from django.urls import path

from .views import (
    LoginView, LogoutView, MeView, RegisterView,
    RegistrationEmailCodeView, TokenRefreshView,
)


urlpatterns = [
    path('email-codes/', RegistrationEmailCodeView.as_view(), name='registration-email-code'),
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', LoginView.as_view(), name='login'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('me/', MeView.as_view(), name='me'),
    path('logout/', LogoutView.as_view(), name='logout'),
]
