# Swagger 使用指南

## 1. 用途

后端使用 `drf-spectacular` 根据 Django REST Framework 路由和 schema 声明生成 OpenAPI 接口文档。Swagger UI 可以查看请求参数、响应结构，并在浏览器中直接调试接口。当前文档包括健康检查和 6 个账号接口。

## 2. 启动后端

在仓库根目录的 PowerShell 中执行：

```powershell
cd .\backend
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

如果尚未安装后端依赖，先执行：

```powershell
cd .\backend
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 3. 文档地址

后端启动后，在浏览器打开：

| 地址 | 用途 |
| --- | --- |
| [http://127.0.0.1:8000/api/docs/](http://127.0.0.1:8000/api/docs/) | Swagger UI：查看接口、提交请求、查看响应 |
| [http://127.0.0.1:8000/api/schema/](http://127.0.0.1:8000/api/schema/) | OpenAPI schema |
| [http://127.0.0.1:8000/api/redoc/](http://127.0.0.1:8000/api/redoc/) | ReDoc：只读浏览接口说明 |

如果 Django 使用 `8001` 等其他端口，把地址中的 `8000` 替换成实际端口。查看这些文档只需要启动后端，不需要启动 Vue 前端。

## 4. 当前接口

| 方法 | 接口 | 认证 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/api/health/` | 不需要 | 健康检查 |
| `POST` | `/api/accounts/email-codes/` | 不需要 | 生成注册验证码 |
| `POST` | `/api/accounts/register/` | 不需要 | 验证邮箱并注册，成功后自动登录 |
| `POST` | `/api/accounts/login/` | 不需要 | 邮箱密码登录 |
| `POST` | `/api/accounts/token/refresh/` | 不需要 | 刷新访问令牌 |
| `GET` | `/api/accounts/me/` | 需要 access token | 获取当前用户 |
| `POST` | `/api/accounts/logout/` | 请求体需提供 refresh token | 注销刷新令牌 |

除健康检查外，账号接口使用统一响应外层：`code`、`message`、`data`、`errors`。健康检查目前仍返回 `{ "status": "ok", "service": "education-api" }`。

## 5. 在 Swagger UI 中调试

1. 在接口列表中展开一个接口，选择 **Try it out**。
2. 按 **Request body** 中显示的格式填写字段，再按 **Execute** 发送请求。
3. 查看 **Responses** 区域中的 HTTP 状态码和返回内容。

### 获取并使用注册验证码

展开 `POST /api/accounts/email-codes/`，提交：

```json
{
  "email": "你的邮箱地址",
  "purpose": "register"
}
```

验证码不会发送到邮箱，也不会出现在 API 响应里。请求成功后，到运行 Django `runserver` 的同一个终端查看打印出的邮件内容并复制 6 位验证码。接口响应中的 `message` 仍沿用“请查收邮件”的旧文案，以终端输出为准。

### 注册并登录

展开 `POST /api/accounts/register/`，填写邮箱、密码、确认密码、终端中取得的验证码和条款同意状态。密码需要至少 8 位并包含数字；后端还会拒绝常见密码、纯数字密码，以及与邮箱信息过于相似的密码。成功后，响应的 `data` 中会包含 `access`、`refresh` 和 `user`。

### 调试需要登录的接口

1. 先调用 `POST /api/accounts/login/` 或完成注册，复制响应中的 `data.access`。
2. 点击 Swagger 页面右上方的 **Authorize**，在 `jwtAuth` 输入框中粘贴 access token。只粘贴 token 本身，不要加 `Bearer ` 前缀；Swagger 会自动添加认证前缀。
3. 授权后调用 `GET /api/accounts/me/`。访问令牌默认有效 15 分钟。
4. 访问令牌过期时，用 `POST /api/accounts/token/refresh/` 提交 `data.refresh`，再用返回的新 `data.access` 重新授权。
5. 退出时调用 `POST /api/accounts/logout/`，请求体提供 `refresh`。前端实际退出流程还会清除浏览器当前标签页中保存的令牌。

## 6. 调试注意事项

- Swagger 的 **Try it out** 会真实调用本地后端：注册会写入本地数据库，注销会使刷新令牌失效。
- 验证码同一邮箱 60 秒后才能重发；同一来源 IP 每小时最多请求 10 次；每个验证码最多允许尝试 5 次。
- 如果接口返回 `401`，检查是否已用有效 access token 授权；如果 Swagger 显示 `404`，确认 Django 正在运行且访问地址端口正确。
- 生成接口结构后端会读取 `backend/apps/accounts/schema.py` 中的响应 schema 和各视图的 `@extend_schema` 声明。新增或修改 API 时应同步更新 schema 声明。

可以从 `backend/` 目录运行以下命令，检查 OpenAPI schema 是否能正常生成：

```powershell
.\.venv\Scripts\python.exe manage.py spectacular --validate --file NUL
```

## 7. 相关文件

| 文件 | 用途 |
| --- | --- |
| `backend/requirements.txt` | Swagger 依赖版本 |
| `backend/config/settings.py` | OpenAPI schema 生成器和文档标题配置 |
| `backend/config/urls.py` | Swagger、OpenAPI、ReDoc 路由 |
| `backend/apps/accounts/schema.py` | 账号接口响应格式 |
| `backend/apps/accounts/views.py` | 账号接口的文档注解 |
| `backend/apps/courses/views.py` | 健康检查接口的文档注解 |

官方参考：[drf-spectacular 快速开始](https://drf-spectacular.readthedocs.io/en/stable/readme.html)。
