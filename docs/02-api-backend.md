# 后端 API 接口约定

## 1. 说明与实现状态

本文记录 Vue 前端调用 Django REST Framework 后端的接口约定。数据方向是**前端发起 HTTP 请求，后端校验并返回响应**；不包含后端主动调用前端。

第一版账号接口均已实现并由前端接入。健康检查接口仍保留旧响应格式；每个接口下方标记了当前状态。

## 2. 统一请求与响应格式

### 请求

- 本地后端默认地址：`http://127.0.0.1:8000`
- API 前缀：`/api/`
- JSON 请求使用 `Content-Type: application/json`。
- 需要登录的请求携带 `Authorization: Bearer <access>`。
- 邮箱去除首尾空格并规范化后再校验和保存。

### 成功响应

所有 JSON 成功响应使用相同的外层结构：

```json
{
  "code": 200,
  "message": "操作成功。",
  "data": {},
  "errors": null
}
```

字段约定：

| 字段 | 含义 |
| --- | --- |
| `code` | 数字状态码，始终与 HTTP 响应状态码相同 |
| `message` | 面向用户的简短说明 |
| `data` | 本次操作返回的数据；无数据时为 `null` |
| `errors` | 字段错误；成功时为 `null` |

### 错误响应

所有错误也使用相同外层结构。`code` 直接填写 HTTP 状态码：

```json
{
  "code": 400,
  "message": "请求参数不合法。",
  "data": null,
  "errors": {
    "email": ["请输入有效的邮箱地址。"]
  }
}
```

没有字段级错误时，`errors` 为 `null`。后端需在 DRF 响应和异常处理中实现这层统一格式；不能让部分接口返回 `detail`、部分返回字段对象、其他接口再返回另一种结构。

### 状态码约定

响应体中的 `code` 是数字，必须与 HTTP 状态码一致。前端根据状态码和当前请求的接口处理流程，字段错误显示 `errors`；`message` 只用于展示提示。

| HTTP 状态与 `code` | 使用场景 |
| --- | --- |
| `200` | 请求成功，包括登录、验证码发送、令牌刷新、获取用户信息和退出登录 |
| `201` | 注册成功并创建用户 |
| `400` | 请求字段格式错误、密码不符合规则、两次密码不一致、验证码错误/过期/已使用、未同意条款 |
| `401` | 登录凭据错误，或访问令牌、刷新令牌缺失、无效、过期 |
| `404` | `/api/` 下请求的接口路径不存在 |
| `405` | 路径存在，但 HTTP 方法不支持 |
| `409` | 提交注册时，邮箱已经有账号 |
| `429` | 验证码发送次数超过限制 |
| `500` | 未预期的服务端错误；响应中不包含堆栈或敏感信息 |
| `503` | 后端终端输出验证码失败 |

## 3. 接口清单

| 方法 | 路径 | 用途 | 状态 |
| --- | --- | --- | --- |
| `GET` | `/api/health/` | 后端健康检查 | 已实现，响应待统一包装 |
| `POST` | `/api/accounts/email-codes/` | 发送注册验证码 | 已实现，前端已接入 |
| `POST` | `/api/accounts/register/` | 验证邮箱并注册，成功后直接登录 | 已实现，前端已接入 |
| `POST` | `/api/accounts/login/` | 邮箱密码登录 | 已实现，前端已接入 |
| `POST` | `/api/accounts/token/refresh/` | 刷新访问令牌 | 已实现，前端已接入 |
| `GET` | `/api/accounts/me/` | 获取当前登录用户 | 已实现，前端已接入 |
| `POST` | `/api/accounts/logout/` | 作废刷新令牌并退出 | 已实现，前端已接入 |

## 4. 接口定义

### 4.1 健康检查

**`GET /api/health/`**  
认证：不需要。  
状态：已实现；当前代码响应还没有统一外层格式。

当前实际响应：

```json
{
  "status": "ok",
  "service": "education-api"
}
```

后续统一包装后的契约响应：

```json
{
  "code": 200,
  "message": "后端服务正常。",
  "data": {
    "status": "ok",
    "service": "education-api"
  },
  "errors": null
}
```

### 4.2 发送注册邮箱验证码

**`POST /api/accounts/email-codes/`**  
认证：不需要。  
状态：已实现，前端已接入。

请求：

```json
{
  "email": "student@example.com",
  "purpose": "register"
}
```

第一版 `purpose` 只支持 `register`。发送验证码时只校验邮箱格式、请求目的和发送频率，不检查邮箱是否已注册。后端会生成验证码邮件并输出到运行 Django 的终端，包括已注册邮箱的请求。

验证码生成成功返回 `200 OK`：

```json
{
  "code": 200,
  "message": "验证码已发送，请查收邮件。",
  "data": {
    "code_valid_for": 600,
    "retry_after": 60
  },
  "errors": null
}
```

当前开发配置使用 Django 控制台邮件后端，因此上面的 `message` 沿用了邮件投递场景的提示文案；验证码实际打印在处理本次请求的 Django 终端中，不会发送到真实邮箱。响应中的 `data` 不包含验证码。

邮箱是否已注册只在提交注册时检查。已经注册的邮箱也会收到此次请求的验证码，但不能用它再创建一个账号。

已实现的规则：验证码为 6 位数字、10 分钟有效、同一邮箱 60 秒后才能重发；同一来源 IP 每小时最多请求 10 次。验证码以哈希形式存储，**不会出现在 API 响应中**，只在 Django 后端终端的邮件内容中显示。注册时最多尝试 5 次，成功注册后验证码立即失效。

错误响应：

| 状态码 | 说明 |
| --- | --- |
| `400` | 邮箱格式错误或 `purpose` 不支持；详情放在 `errors` |
| `429` | 发送过于频繁；`data` 中返回 `retry_after` |
| `503` | 后端输出验证码失败 |

`429` 示例：

```json
{
  "code": 429,
  "message": "请求过于频繁，请稍后再试。",
  "data": {
    "retry_after": 60
  },
  "errors": null
}
```

### 4.3 注册并自动登录

**`POST /api/accounts/register/`**  
认证：不需要。  
状态：已实现，前端已接入。

请求：

```json
{
  "email": "student@example.com",
  "password": "用户输入的密码",
  "password_confirm": "用户输入的密码",
  "verification_code": "123456",
  "accepted_terms": true
}
```

注册请求必须包含邮箱、密码、确认密码、验证码和是否同意条款。后端验证 `password_confirm` 与 `password` 一致，再创建账号。密码至少 8 位并包含数字；此外还使用 Django 密码校验器，拒绝常见密码、纯数字密码，以及与邮箱信息过于相似的密码。因此满足页面“至少 8 位并包含数字”的提示仍不一定通过后端校验；具体失败原因放在 `errors.password`。验证码最多允许输错 5 次，过期或用过后需重新获取。

注册成功返回 `201 Created`，并直接签发访问令牌和刷新令牌：

```json
{
  "code": 201,
  "message": "注册成功。",
  "data": {
    "access": "访问令牌",
    "refresh": "刷新令牌",
    "user": {
      "id": 12,
      "email": "student@example.com"
    }
  },
  "errors": null
}
```

错误响应：

| 状态码 | 说明 |
| --- | --- |
| `400` | 邮箱格式、密码、确认密码、验证码或条款校验失败；具体字段放在 `errors` |
| `409` | 邮箱已注册，请直接登录 |

邮箱重复示例：

```json
{
  "code": 409,
  "message": "该邮箱已注册，请直接登录。",
  "data": null,
  "errors": null
}
```

字段错误示例（密码校验错误的具体提示会随触发的规则而变化）：

```json
{
  "code": 400,
  "message": "请求参数不合法。",
  "data": null,
  "errors": {
    "verification_code": ["验证码错误或已过期，请重新获取。"],
    "password": ["密码至少 8 位，并且需要包含数字。"],
    "password_confirm": ["两次输入的密码不一致。"]
  }
}
```

后端只在提交注册时检查邮箱是否已注册，并依靠数据库唯一约束处理并发注册。已注册邮箱提交注册时返回 `409`。

### 4.4 邮箱密码登录

**`POST /api/accounts/login/`**  
认证：不需要。  
状态：已实现，前端已接入。

请求：

```json
{
  "email": "student@example.com",
  "password": "用户输入的密码"
}
```

成功返回 `200 OK`，`data` 与注册成功响应相同：

```json
{
  "code": 200,
  "message": "登录成功。",
  "data": {
    "access": "访问令牌",
    "refresh": "刷新令牌",
    "user": {
      "id": 12,
      "email": "student@example.com"
    }
  },
  "errors": null
}
```

邮箱不存在、密码错误或账号不可用时统一返回 `401 Unauthorized`，避免泄露账号状态：

```json
{
  "code": 401,
  "message": "邮箱或密码错误。",
  "data": null,
  "errors": null
}
```

### 4.5 刷新访问令牌

**`POST /api/accounts/token/refresh/`**  
认证：请求体提供有效的刷新令牌。  
状态：已实现，前端已接入。

请求：

```json
{
  "refresh": "刷新令牌"
}
```

成功返回 `200 OK`：

```json
{
  "code": 200,
  "message": "访问令牌已刷新。",
  "data": {
    "access": "新的访问令牌"
  },
  "errors": null
}
```

刷新令牌无效、过期或已作废时返回 `401 Unauthorized`：

```json
{
  "code": 401,
  "message": "登录状态已过期，请重新登录。",
  "data": null,
  "errors": null
}
```

### 4.6 获取当前用户

**`GET /api/accounts/me/`**  
认证：需要有效的访问令牌。  
状态：已实现，前端已接入。

请求头：

```http
Authorization: Bearer <access>
```

成功返回 `200 OK`：

```json
{
  "code": 200,
  "message": "获取用户信息成功。",
  "data": {
    "id": 12,
    "email": "student@example.com"
  },
  "errors": null
}
```

缺少访问令牌、令牌无效或过期时返回 `401 Unauthorized`：

```json
{
  "code": 401,
  "message": "请先登录。",
  "data": null,
  "errors": null
}
```

### 4.7 退出登录

**`POST /api/accounts/logout/`**  
认证：请求体提供刷新令牌。  
状态：已实现，前端已接入。

请求：

```json
{
  "refresh": "刷新令牌"
}
```

成功返回 `200 OK`，响应仍使用统一结构：

```json
{
  "code": 200,
  "message": "已退出登录。",
  "data": null,
  "errors": null
}
```

后端将刷新令牌加入黑名单；前端清除当前标签页 `sessionStorage` 中的 `access` 和 `refresh`。已经签发的访问令牌在到期前仍可能有效，当前有效期为 15 分钟；刷新令牌有效期为 7 天。

无效或已过期的刷新令牌返回 `401 Unauthorized`，前端无论成功或令牌已失效，都应清除本地令牌并返回登录页。

## 5. 前端页面对应关系

| 页面操作 | 调用接口 | 成功后的前端行为 |
| --- | --- | --- |
| 登录邮箱和密码 | `POST /api/accounts/login/` | 保存令牌，进入工作区 |
| 注册页发送验证码 | `POST /api/accounts/email-codes/` | 成功时展示发送提示，并按 `data.retry_after` 倒计时 |
| 提交注册时发现邮箱已注册 | `POST /api/accounts/register/` | 收到 `409` 后提示用户直接登录 |
| 提交邮箱、密码、确认密码、验证码和条款勾选状态 | `POST /api/accounts/register/` | 保存令牌，进入工作区 |
| 初始化或恢复登录用户信息 | `GET /api/accounts/me/` | 展示当前邮箱 |
| 访问令牌过期 | `POST /api/accounts/token/refresh/` | 更新 access 后重试原请求 |
| 退出登录 | `POST /api/accounts/logout/` | 清除令牌，跳转登录页 |

“验证码登录”和“忘记密码”不属于第一版接口范围，前端已隐藏这两个入口。

注册页已增加“确认密码”输入框，并将其值作为 `password_confirm` 发送给注册接口。

## 6. 本地启动与验证码联调

### 本地启动后端（PowerShell）

以下命令均从仓库根目录执行。首次运行先创建虚拟环境、安装依赖并迁移数据库：

```powershell
cd .\backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
```

之后日常启动后端：

```powershell
cd .\backend
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

启动后可访问 `http://127.0.0.1:8000/api/health/`。当前开发配置使用 Django 控制台邮件后端；请求验证码后，验证码会显示在这个运行 `runserver` 的终端里，不需要 SMTP 设置，也不会投递到真实邮箱。

若 8000 端口已有服务，可以改用 8001：

```powershell
cd .\backend
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8001
```

使用 8001 端口时，前端开发服务器也要设置 `VITE_API_TARGET=http://127.0.0.1:8001`，否则 `/api` 仍会代理到默认的 8000 端口。具体前端启动命令见 `backend/README.md`。

不要在同一端口同时运行多个 Django 进程，否则请求可能由旧进程处理。发送验证码接口返回 `200` 后，查看对应 Django 服务终端中刚打印的邮件内容，复制其中的 6 位验证码。

### 验证码联调说明

验证码只输出在后端终端，不会显示在浏览器或发到邮箱。若终端没有新邮件内容，先确认前端代理的目标端口和当前 Django 服务端口一致，并查看后端是否收到 `POST /api/accounts/email-codes/` 请求。真实邮箱投递暂不在当前版本范围内。

Vite 开发服务器默认将 `/api` 请求代理到本地 Django 的 `http://127.0.0.1:8000`，也可通过 `VITE_API_TARGET` 指定其他后端地址。启动前后端开发服务器即可从注册页发起验证码请求。生产环境部署时仍需要让同一路径转发到 Django。验证码接口的按 IP 限流目前使用 Django 默认缓存；多进程或多实例部署前，需要换成共享缓存。

### Swagger / OpenAPI 接口文档

启动 Django 后，在浏览器打开以下地址查看当前 API 格式：

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:8000/api/docs/` | Swagger UI，可查看接口并填写参数进行调试 |
| `http://127.0.0.1:8000/api/schema/` | OpenAPI schema 原始文档 |
| `http://127.0.0.1:8000/api/redoc/` | ReDoc 格式的只读接口文档 |

如果 Django 使用其他端口（例如 `8001`），将地址中的端口一并替换。当前 schema 描述健康检查和 6 个账号接口，包括请求字段、成功响应、常见错误响应及 JWT Bearer 认证要求。

Swagger 的启动与调试步骤见 [`03-swagger.md`](./03-swagger.md)。

## 7. 当前代码状态

- 已实现：健康检查与全部 6 个账号接口；账号路由位于 `backend/apps/accounts/urls.py`，业务代码位于同一应用的 `views.py`、`services.py` 和 `serializers.py`。健康检查仍返回未包装的旧格式。
- 用户模型以唯一邮箱为登录标识，保存同意条款的时间；注册验证码在成功创建用户后标记为已使用。JWT 刷新令牌黑名单已启用。
- 前端登录、注册、获取用户和退出已接入账号 API；令牌存储在当前标签页 `sessionStorage` 中。
- 后续仍需统一 `/api/` 下未匹配路径和未捕获服务端错误的响应包装，并在多实例部署前为验证码限流配置共享缓存。
