# API Key 配置指引

本项目需要 Gemini、USDA FoodData Central 和 Supabase 三组配置。注册完成后，把 `.env.example` 复制为本地 `.env` 并填写真实值；不要把 `.env`、截图或真实 key 提交到 Git，也不要在聊天或日志中粘贴 key。

## 1. Gemini API

入口：[Google AI Studio API keys](https://aistudio.google.com/apikey)

1. 使用 Google 账号登录 AI Studio。
2. 选择或创建一个 Google Cloud 项目。
3. 在 API keys 页面创建 key；开发阶段选择可用的免费档即可。
4. 将 key 填入本地 `.env` 的 `GEMINI_API_KEY`。
5. 若控制台提供 key 限制，按项目需要限制可用 API，并定期轮换泄露或不再使用的 key。

## 2. USDA FoodData Central API

入口：[FoodData Central API key signup](https://fdc.nal.usda.gov/api-key-signup.html)

1. 打开注册页，填写姓名和邮箱地址。
2. 提交后在邮件中取得免费的 API key；若未收到，检查垃圾邮件。
3. 将 key 填入本地 `.env` 的 `USDA_FDC_API_KEY`。
4. 不要把 key 写入菜谱、测试 fixture 或客户端代码。

## 3. Supabase

入口：[Supabase](https://supabase.com)

1. 注册或登录后创建一个免费档项目，选择合适的区域并妥善保存数据库密码。
2. 等待项目初始化完成，进入项目的 API 设置页面。
3. 记录 Project URL，填入本地 `.env` 的 `SUPABASE_URL`。
4. 记录公开客户端使用的 anon/publishable key，填入 `SUPABASE_ANON_KEY`。
5. 记录仅供可信服务端使用的 service role/secret key，填入 `SUPABASE_SERVICE_ROLE_KEY`。
6. service role key 能绕过行级权限，只能保留在服务端环境中，绝不能放入浏览器、移动端、固件、截图或版本库。

## 本地检查

配置后只检查变量是否存在，不要打印实际值：

```bash
.venv/bin/python -c "from pathlib import Path; required=['GEMINI_API_KEY','USDA_FDC_API_KEY','SUPABASE_URL','SUPABASE_ANON_KEY','SUPABASE_SERVICE_ROLE_KEY']; values=dict(line.split('=',1) for line in Path('.env').read_text().splitlines() if '=' in line and not line.lstrip().startswith('#')); missing=[k for k in required if not values.get(k)]; print('configured' if not missing else 'missing: '+', '.join(missing))"
```
