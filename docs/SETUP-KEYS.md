# API Key 配置指引

本项目按功能选用 Gemini、科大讯飞、USDA FoodData Central 和 Supabase。注册完成后，把 `.env.example` 复制为本地 `.env` 并填写真实值；不要把 `.env`、截图或真实 key 提交到 Git，也不要在聊天或日志中粘贴 key。

## 1. Gemini API

入口：[Google AI Studio API keys](https://aistudio.google.com/apikey)

1. 使用 Google 账号登录 AI Studio。
2. 选择或创建一个 Google Cloud 项目。
3. 在 API keys 页面创建 key；开发阶段选择可用的免费档即可。
4. 将 key 填入本地 `.env` 的 `GEMINI_API_KEY`。
5. 若控制台提供 key 限制，按项目需要限制可用 API，并定期轮换泄露或不再使用的 key。

## 2. 科大讯飞流式语音与机器翻译

入口：[讯飞开放平台控制台](https://console.xfyun.cn/)

1. 创建 WebAPI 应用，开通“在线语音合成（流式版）”，取得 `APPID`、`APIKey` 和 `APISecret`。
2. 将三项分别填入 `IFLYTEK_APP_ID`、`IFLYTEK_API_KEY`、`IFLYTEK_API_SECRET`。
3. 在控制台为应用添加实际使用的发音人。中文默认参数为 `x4_xiaoyan`；若账号显示的参数不同，用 `IFLYTEK_TTS_VOICE_ZH_CN` 覆盖。
4. 英文或其他语言必须开通对应发音人，并填写相应变量，例如 `IFLYTEK_TTS_VOICE_EN_US`。TTS 的语种由发音人决定，不是仅靠语言代码切换。
5. 非中文旁白还需要开通“机器翻译”。若该产品给出独立密钥，则填写 `IFLYTEK_MT_APP_ID`、`IFLYTEK_MT_API_KEY`、`IFLYTEK_MT_API_SECRET`；否则留空并复用通用三项。

在线 TTS 使用签名 WebSocket URL。代码和日志不得保存完整 URL，因为查询参数中包含临时鉴权信息。

只检查讯飞配置是否存在：

```bash
.venv/bin/python -c "from server.iflytek_config import iflytek_is_configured; print('configured' if iflytek_is_configured() else 'missing')"
```

## 3. USDA FoodData Central API

入口：[FoodData Central API key signup](https://fdc.nal.usda.gov/api-key-signup.html)

1. 打开注册页，填写姓名和邮箱地址。
2. 提交后在邮件中取得免费的 API key；若未收到，检查垃圾邮件。
3. 将 key 填入本地 `.env` 的 `USDA_FDC_API_KEY`。
4. 不要把 key 写入菜谱、测试 fixture 或客户端代码。

## 4. Supabase

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
