from rest_framework.decorators import api_view
from rest_framework import serializers
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema


class HealthCheckSerializer(serializers.Serializer):
    status = serializers.CharField()
    service = serializers.CharField()


@extend_schema(
    tags=['系统'],
    summary='健康检查',
    responses={200: HealthCheckSerializer},
)
@api_view(['GET'])
def health_check(request):
    return Response({'status': 'ok', 'service': 'education-api'})
