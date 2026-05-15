# DashPi

## 關於 DashPi

DashPi 是一個開源、可自訂的 Raspberry Pi 智慧顯示器專案。它支援多種顯示器類型，並可在安裝後依照設定或硬體偵測選擇顯示方式。

| 顯示器類型 | 支援內容 |
|---|---|
| **LCD**，例如 Waveshare 7 吋 1024x600 HDMI IPS | 全彩、快速刷新、觸控、背光與亮度排程 |
| **E-Ink**，Pimoroni Inky | 類紙質感、無眩光、低功耗，可透過 Inky library 偵測 |
| **E-Ink**，Waveshare e-Paper | 支援多種尺寸與色彩版本，包含黑白、雙色與 Spectra 6 |

所有顯示器類型共用同一套 plugin、生態系、Web UI 與設定檔，差異只在最後的畫面輸出方式。

**功能特色**：
- **顯示器支援**：支援 LCD、Pimoroni Inky 與 Waveshare e-Paper
- **Web 介面**：可在區域網路中設定顯示器、管理 plugins、建立 loops
- **25 個內建 plugins**：天氣、時鐘、AI 圖片、新聞、股票、藝術博物館、Plugin Manager、ISS tracker 等
- **排程 loops**：不同時間顯示不同 plugins
- **多裝置友善**：同一網路中可放多台 DashPi，每台有自己的名稱與設定，可透過 `hostname.local` 存取
- **自我更新**：可從 Web UI 檢查並套用更新
- **開源**：可自行修改、擴充，或建立自己的 plugin

**內建 plugins 包含**：Weather、Clock、Plugin Manager、AI Image、AI Text、NASA APOD、Art Museum、Stocks、ISS Tracker、Flight Tracker、ShazamPi、Calendar、Newspaper、Comics、RSS、Image Upload、Image Album、Image URL、Countdown、GitHub、To-Do List、Unsplash、Year Progress、Wikipedia POTD 等。

自訂 plugin 的文件請參考 [Building Plugins](./docs/building_plugins.md)。

## 畫面截圖

| Web UI，LCD | Web UI，E-Ink |
|---|---|
| ![LCD Dashboard](docs/images/dashpi_webui_lcd.jpg) | ![E-Ink Dashboard](docs/images/dashpi_webui_eink.jpg) |

## 硬體需求

- Raspberry Pi 4、3 或 Zero 2 W
- MicroSD 卡，至少 8 GB
- 下列其中一種顯示器：
    - **LCD**：Waveshare 7 吋 1024x600 HDMI IPS Display，支援觸控
    - **Inky e-Paper**：Pimoroni Inky Impression 13.3 吋、7.3 吋、5.7 吋、4 吋，或 Inky wHAT 4.2 吋
    - **Waveshare e-Paper**：Spectra 6、黑白、雙色，以及多種尺寸
        - 注意：目前不支援 IT8951-based 顯示器

## 安裝

**快速安裝，單行指令**：

```bash
curl -sSL https://raw.githubusercontent.com/tinganhsu/DashPi/main/install/bootstrap.sh | sudo bash
```

**或逐步安裝**：

1. 安裝 git：

    ```bash
    sudo apt-get update && sudo apt-get install -y git
    ```

2. Clone 專案：

    ```bash
    git clone https://github.com/tinganhsu/DashPi.git
    ```

3. 進入專案目錄：

    ```bash
    cd DashPi
    ```

4. 執行安裝腳本：

    ```bash
    sudo bash install/install.sh
    ```

    如果使用 Waveshare e-Paper，請指定 Waveshare driver 名稱。例如 Waveshare 7.3 吋 F：

    ```bash
    sudo bash install/install.sh -W epd7in3f
    ```

安裝完成後，腳本會詢問是否重新啟動 Raspberry Pi。重新啟動後即可透過瀏覽器開啟 DashPi Web UI。

**注意**：安裝腳本需要 sudo 權限。建議使用乾淨的 Raspberry Pi OS 安裝環境。安裝器會自動啟用 SPI 和 I2C、在低記憶體裝置上擴充 swap，並安裝相依套件。

**Waveshare 注意事項**：Waveshare e-Paper 無法像 Pimoroni Inky 一樣可靠自動偵測，必須使用 model name 設定。`-W epd7in3f` 會下載對應 driver，並將 `display_type` 寫入 `src/config/device.json`。

**Pi Zero 使用者**：低記憶體裝置安裝時間較久，約 15 到 20 分鐘。安裝器會自動管理 swap，並分批安裝 Python 套件以避免記憶體不足。

## 已安裝後手動設定 Waveshare

如果你已經安裝 DashPi，但當時沒有使用 `-W epd7in3f`，通常不需要重灌。可以手動下載 driver、修改設定檔，然後重開機。

先停止 DashPi service：

```bash
sudo systemctl stop dashpi
```

進入你實際 clone DashPi 的目錄，以下以 `~/DashPi` 為例：

```bash
cd ~/DashPi
mkdir -p src/display/waveshare_epd
```

下載 Waveshare 7.3 吋 F 的 driver：

```bash
curl -fsSL -o src/display/waveshare_epd/epd7in3f.py \
  https://raw.githubusercontent.com/waveshareteam/e-Paper/master/RaspberryPi_JetsonNano/python/lib/waveshare_epd/epd7in3f.py

curl -fsSL -o src/display/waveshare_epd/epdconfig.py \
  https://raw.githubusercontent.com/waveshareteam/e-Paper/refs/heads/master/RaspberryPi_JetsonNano/python/lib/waveshare_epd/epdconfig.py
```

編輯 DashPi 設定檔：

```bash
nano src/config/device.json
```

將 `display_type` 設成：

```json
"display_type": "epd7in3f"
```

如果設定檔內已經有 `"resolution"`，但不是目前 e-Paper 的正確解析度，建議先刪除 `"resolution"` 那一行。DashPi 會從 Waveshare driver 讀取解析度並重新寫入。

接著確認 Raspberry Pi 的 boot config。Bookworm 通常是 `/boot/firmware/config.txt`，較舊系統可能是 `/boot/config.txt`：

```bash
sudo nano /boot/firmware/config.txt
```

確認有這行：

```ini
dtoverlay=spi0-2cs
```

如果原本有 `dtoverlay=spi0-0cs`，使用 Waveshare e-Paper 時建議註解掉或改成 `dtoverlay=spi0-2cs`，避免 SPI chip-select overlay 衝突。

最後重新啟動：

```bash
sudo reboot
```

重開機後可檢查 service 狀態與 log：

```bash
sudo systemctl status dashpi
journalctl -u dashpi -n 100 --no-pager
```

如果你的 hostname 是 `inkypi`，安裝器可能會把 service 名稱設為 `inkypi`。這種情況下，請把上述指令中的 `dashpi` 換成 `inkypi`。

## 更新

DashPi 可以直接從 Web UI 的 **Settings** 頁面更新，點選「Check for Updates」即可。

如果你是從本 repo 安裝，Web UI 更新會使用目前設定的 git remote。可先確認 remote 是否指向此版本：

```bash
cd DashPi
git remote -v
```

應該看到：

```text
origin  https://github.com/tinganhsu/DashPi.git (fetch)
origin  https://github.com/tinganhsu/DashPi.git (push)
```

也可以手動更新：

```bash
cd DashPi
git pull
sudo bash install/install.sh
```

如果使用 Waveshare e-Paper，手動更新後建議保留或重新指定 model：

```bash
sudo bash install/install.sh -W epd7in3f
```

## 從原始 fork 更新到此版本

如果你先前已經從原始 DashPi fork 安裝，例如 `SHagler2/DashPi`，不需要整台重灌。可以把現有 repository 的 git remote 改到此版本，再拉取更新。

先進入你 Pi 上的 DashPi 目錄：

```bash
cd ~/DashPi
```

確認目前 remote：

```bash
git remote -v
```

如果看到 `SHagler2/DashPi`，將 `origin` 改成此 repo：

```bash
git remote set-url origin https://github.com/tinganhsu/DashPi.git
```

如果你使用 SSH，也可以改成：

```bash
git remote set-url origin git@github.com:tinganhsu/DashPi.git
```

接著拉取此版本：

```bash
git fetch origin
git checkout main
git pull --ff-only origin main
```

更新後重新執行安裝腳本，讓 systemd service、Python dependencies、vendor files 與 boot 設定同步更新：

```bash
sudo bash install/install.sh
```

如果你使用 Waveshare e-Paper，請帶上 model name：

```bash
sudo bash install/install.sh -W epd7in3f
```

若 `git pull --ff-only` 失敗，通常代表你本機有修改過檔案。先查看差異：

```bash
git status
```

若你只想保留目前設定檔與 `.env`，可先備份以下檔案，再處理 git 衝突：

```bash
cp src/config/device.json ~/dashpi-device.json.bak
cp .env ~/dashpi-env.bak
```

不要直接刪除或覆蓋 `src/config/device.json` 與 `.env`，這兩個檔案通常包含你的裝置設定、loops 與 API keys。

## 解除安裝

```bash
sudo bash install/uninstall.sh
```

## 從 InkyPi 轉移

DashPi v2.0 是 [InkyPi](https://github.com/SHagler2/InkyPi) 的後續 fork。既有的 e-ink 硬體仍可使用，但 Waveshare e-Paper 需要明確設定 model name，例如 `epd7in3f`。既有 plugins、loops 與 API keys 可透過複製 `device.json` 和 `.env` 到新安裝環境來轉移。

## 授權

本專案採 GPL 3.0 License 發布，詳情請見 [LICENSE](./LICENSE)。

本專案包含字型與 icons，部分素材有各自的授權與 attribution 要求。請參考 [Attribution](./docs/attribution.md)。

## 致謝

DashPi fork 自 fatihak 的 [InkyPi](https://github.com/fatihak/InkyPi)。

Plugin Manager 改寫自 RobinWts 的 [InkyPi-Plugin-PluginManager](https://github.com/RobinWts/InkyPi-Plugin-PluginManager)，原作者為 [RobinWts](https://github.com/RobinWts)。
