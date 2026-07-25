# ESP32 本地配置指南(NomaCook)

> 怎么把 ESP32 摄像头配好、连上你自己的后端。
> 参考了幻尔"AI 小智"教程(`3.7 AI小智.pdf`),但要先讲清楚一件事:
> **小智那套和我们的架构不一样,别照抄。** 下面第 1 节解释为什么。

## 1. 先分清两条路

| | 小智 (xiaozhi-esp32) | NomaCook(我们) |
|---|---|---|
| AI 在哪 | 跑在 ESP32 本机 + xiaozhi.me 云 | 全在我们自己的后端(电脑/云) |
| 视觉 | 喊一声拍一张图,发云端 VLM | ESP32 连续推流 5-10 FPS,后端持续看 |
| 绑定 | 绑到 xiaozhi.me 智能体 | 只连我们自己的 Device Gateway |
| 唤醒词 | "你好小智",本机唤醒 | 暂不做本机唤醒,语音在后端 |

结论:小智固件**不能直接刷上去用**,它不会把视频流给你的后端。它对我们的价值是
一份 ESP32 的**通用教学**:开发环境、烧录、配网这几样功夫,换到哪块板子都一样。
真正要跑的固件见第 3 节。

## 2. 从小智 PDF 学到的、能直接用的三样

### 2.1 开发环境:VSCode + ESP-IDF
以后要给 ESP32-S3 写/改任何固件,都要这套。步骤(PDF 3.7.2):
1. 装 VSCode,装 ESP-IDF 插件(选一个稳定版,如 ESP-IDF v5.x)。
2. Type-C 线把 ESP32-S3 连电脑。
3. VSCode 打开固件工程文件夹。**路径必须全英文**,中文路径会编译失败。
4. 左下角选:ESP-IDF 版本、烧录方式 `UART`、端口(Mac 上是 `/dev/cu.usbserial-*` 或
   `/dev/cu.usbmodem*`,不是 Windows 的 COMx)、芯片 `esp32s3`。
5. 编译(build)→ 烧录(flash)→ 监视串口(monitor)。命令行等价:
   `idf.py build flash monitor`。

### 2.2 SoftAP 配网(最值得抄的交互)
小智让板子第一次上电时**自己开一个热点**,你连上去用网页填 WiFi。这个模式很适合
现场,评委的场地 WiFi 你事先不知道密码,靠这个当场配。原理(PDF 3.7.3):
1. 板子没存过可用 WiFi 时,自动进配网模式,开一个无密码热点(小智叫 `Xiaozhi-xxxx`)。
2. 手机/电脑连这个热点,浏览器开 `http://192.168.4.1`。
3. 网页里填你要它连的 WiFi 名和密码,点连接,板子重启并连上。
4. 长按 BOOT 键可以随时手动重新进配网模式。

**用手机热点当那个 WiFi。** 普通路由器常把设备互相隔离,板子在线你也访问不到它;
手机热点没这问题(你们 CLAUDE.md 也把"现场 WiFi"列为 demo 第一死因)。

### 2.3 板级抽象长什么样(看个眼熟)
小智的 `HiwonderExploit_S3.cc` 是一个板级硬件抽象:SPI 接屏、I2C 接音频和摄像头、
XL9555 扩 IO、初始化相机和屏幕、注册按键。你以后要给 DFR1154 写自定义固件时,
结构是一样的:一个 board 文件把相机、屏幕、音频、按键都初始化好。现在不用写,
知道长这样即可。

## 3. DFR1154 实际要跑的路径(推荐,最短)

我们不需要在板子上跑 AI,所以别碰 ESP-IDF 那套复杂编译。直接用 DFRobot 的
CameraWebServer 例子,让板子把 MJPEG 流推出来,后端来读:

1. Arduino IDE 打开 [DFR1154_Examples](https://github.com/DFRobot/DFR1154_Examples)
   里的 CameraWebServer,填 WiFi 名和密码(或直接刷 DFRobot 给的 .bin)。
2. WiFi 用手机热点,让笔记本和板子在同一个热点下。
3. 板子启动,串口会打印它的 IP,比如 `192.168.43.100`。
4. 浏览器开 `http://<板子IP>/`,点 "Start Stream",看到画面就说明通了。
5. 流地址一般是 `http://<板子IP>:81/stream`。先用探针确认电脑能读:
   ```bash
   .venv/bin/python harness/probe_stream.py --url http://192.168.43.100:81/stream
   ```
6. 通了就喂给 live demo,和用 webcam 一模一样,只是 source 换成 URL:
   ```bash
   .venv/bin/python harness/live_demo.py --source http://192.168.43.100:81/stream
   ```

软件这边不用改:`server/live/frame_source.py` 的 `CameraStreamSource` 已经能读这种
URL,还带断线重连。

## 4. 以后可以从小智借、但现在不做的

- **配网 UX**:把 2.2 那个 SoftAP 网页配网做进我们自己的固件,现场配 WiFi 更顺。
- **唤醒词**:小智的 menuconfig 能自定义唤醒词(PDF 3.7.7,拼音格式如 `ni hao xiao zhi`,
  阈值 1-99 调灵敏度)。等我们要做本机语音唤醒时可参考,但我们的语音主要在后端。
- **绝对不要**:把设备绑到 xiaozhi.me、在板子上跑云端对话、走"拍单张图"那套。
  这三样和我们"连续流 + 自有后端"的架构冲突。

## 5. 常见坑

- Mac 上端口不是 COMx,是 `/dev/cu.usbserial-*` 或 `/dev/cu.usbmodem*`,`ls /dev/cu.*` 查。
- 中文路径编译失败,工程放全英文路径下。
- 浏览器能看流但探针读不到:多半端口/路径不对,试 `:81/stream`。
- 浏览器都看不到:笔记本和板子没在同一个能互通的网里,换手机热点。
- 板子反复重启进配网:它连不上你填的 WiFi,检查名字密码,信号别太弱。
