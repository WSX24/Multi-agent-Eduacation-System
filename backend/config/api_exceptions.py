import math

from rest_framework.exceptions import Throttled, ValidationError
from rest_framework.views import exception_handler


def _field_errors(details):
    if not isinstance(details, dict):
        return {'non_field_errors': [str(details)]}
    return {
        field: [str(item) for item in value] if isinstance(value, list) else [str(value)]
        for field, value in details.items()
    }


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None

    data = None
    errors = None
    if isinstance(exc, ValidationError):
        message = '请求参数不合法。'
        errors = _field_errors(response.data)
    elif isinstance(exc, Throttled):
        message = '请求过于频繁，请稍后再试。'
        data = {'retry_after': math.ceil(exc.wait)} if exc.wait is not None else None
    elif response.status_code == 405:
        message = '请求方法不支持。'
    elif response.status_code == 404:
        message = '请求的接口不存在。'
    elif response.status_code == 400:
        message = '请求格式不正确。'
    elif response.status_code == 401:
        message = '请先登录。'
    else:
        message = str(getattr(exc, 'detail', '请求失败。'))

    response.data = {
        'code': response.status_code,
        'message': message,
        'data': data,
        'errors': errors,
    }
    return response
