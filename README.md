<h1 align="center">
    <samp>Telegram Cloner </samp>
</h1>

<p align="center">
    A script to copy all content from a Telegram channel.  <br />
    It retrieves and saves all messages, including text, images, and links, from a specified Telegram channel for easy backup and access.
</p>



---

## ✨ Features

- 📋 Clone any **Telegram channel or group** (supports both public & private).
- 🔄 You **must be a member** of the source channel or group to copy its content.
- 🛠 Automatically handles text, images, PDFs, and videos for efficient backup.

---

### 📂 Clone this Repository

```sh
git clone https://github.com/darshitmachhi/Telegram-cloner.git

cd Telegram-cloner
```

## 🛠 Configuration

### 1. Set Up Your Environment File

Copy the template file to create your `.env` configuration:

```bash
cp .env.example .env
```

Open `.env` in your text editor and fill in your credentials:

- `API_ID` and `API_HASH` - Get these from [my.telegram.org](https://my.telegram.org/)
- `PHONE` - Your phone number with country code (e.g. `+1234567890`)
- `SOURCE_CHANNEL` & `DEST_CHANNEL` - Source and target channel usernames or numeric IDs.

## 🐍 Virtual Environment Setup

It's highly recommended to use a virtual environment to avoid dependency conflicts.

### 1. Create a Virtual Environment

**Windows**

```sh
python -m venv .venv
```

**macOS and Linux**

```sh
python3 -m venv .venv
```

### 2. Activate Virtual Environment

**Windows**

```powershell
.\.venv\Scripts\activate
```

**macOS and Linux**

```bash
source .venv/bin/activate
```

## 📦 Install Dependencies

Install all required dependencies (including Telethon, Flask, tqdm, and Cryptg):

```bash
pip install -r requirements.txt
```

## 🚀 Running the App

### Option A: Web Interface Dashboard (Default)

Launch the web app and open `http://localhost:5000` in your browser:

**Windows**

```powershell
python src/telegram_clone/main.py
```

**macOS and Linux**

```bash
python3 src/telegram_clone/main.py
```

### Option B: Command Line Interface (CLI)

Run directly in the console with CLI options:

```bash
# Start interactive CLI mode
python src/telegram_clone/main.py --cli

# High-speed batch forward with anti-ban pacing (100 msgs/batch, 5s delay)
python src/telegram_clone/main.py --cli --mode forward --batch-delay 5.0 --max-messages 100

# Clone specific forum topics
python src/telegram_clone/main.py --cli --source-topic 7 --dest-topic 12
```

## 🛠 Troubleshooting

- **Database Lock**: The database is patched to use WAL mode. If you see lock warnings, make sure only one instance (either Web UI or CLI) is running at the same time.
- **Message ID Invalid**: The cloner automatically detects system service messages (e.g. title changes, pins) and skips them without getting stuck in retry loops.

## ❤️ Support

If you find this project useful, please ⭐️ star the repository to show your support!

Made with ❤️ by darshitmachhi
