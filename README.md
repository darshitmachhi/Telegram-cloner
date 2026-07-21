# Telegram-clone-old-messages
📨 Clone old Telegram messages from a source chat to a destination chat while preserving chronological order. 🚀 Supports text, media, and documents, with resume capability, rate limiting, progress tracking, and reliable error handling for seamless chat migration and archival.
# [Open Source] High-Speed Telegram Channel & Forum Topic Forwarder with Anti-Ban Pacing, Web UI & Resume Support

Hey everyone! 👋

I wanted to share an updated, open-source tool for cloning/forwarding Telegram channels, groups, and **Forum Topics** with server-side speed, zero local media downloads, and built-in anti-ban pacing.

Whether you're backing up course groups, organizing forum threads, or migrating large channels, this tool handles everything with high-speed batch forwarding and a clean Web Dashboard.

---

### ✨ Key Features

- **⚡ Server-Side Plain Forwarding**: Forwards messages directly on Telegram's servers (`ForwardMessagesRequest`). **Zero local media downloads**, zero disk space required, and original media quality preserved.
- **📌 Telegram Forum Topics Support**: Filter source topic threads and target specific destination topics in Telegram Forum Supergroups.
- **🚀 High-Speed Batch Engine**: Sends up to **100 messages per single Telegram API call** (50x–100x faster than single-message forwarders).
- **🛡️ Anti-Ban Batch Pacing**: Built-in inter-batch delay (default: `5.0s`) to prevent rate limits (`FloodWait`) or account bans.
- **⏸️ Automatic Pause & Resume**: All progress is tracked per message ID in a database. If paused or restarted, it **never starts from scratch**—it automatically skips sent messages and resumes right from where it left off.
- **🔒 SQLite WAL Concurrency**: Configured with WAL journal mode & 60s busy-timeouts to prevent `database is locked` errors.
- **🎨 Glassmorphism Web UI**: Includes a real-time SSE progress monitor, live activity console, and visual transfer mode tiles (`http://localhost:5000`).

---

## 🛠️ Step-by-Step Setup Guide

### Step 1: Clone the Repository & Install Dependencies

```bash
git clone https://github.com/darshitmachhi/telegram-clone.git
cd telegram-clone

# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# Install requirements
pip install -r requirements.txt

Step 2: Get Your Telegram API Credentials
Go to https://my.telegram.org and log in with your phone number.
Click API Development Tools.
Create an application (enter any short name/title).
Copy your api_id and api_hash.
Step 3: Configure Your .env File
Copy .env.example to create .env:

bash


cp .env.example .env
Open .env in any text editor and fill in your credentials:

env


API_ID=12345678
API_HASH=your_api_hash_here
PHONE=+1234567890
Step 4: Launching the App
Option A: Web Interface (Recommended)
Start the web server:

bash


python src/telegram_clone/main.py
Open http://localhost:5000 in your browser.
Select your Source Channel (and optional Source Forum Topic).
Select your Destination Channel (and optional Destination Forum Topic).
Choose Plain Forward mode.
Set Batch Size Limit (e.g. 100) and Anti-Ban Delay (e.g. 5 seconds).
Click ▶ Start Clone.
Option B: Command Line Interface (CLI)
Run high-speed batch forwarding directly in CLI mode:

bash


# Forward 100 messages with 5-second anti-ban delay
python src/telegram_clone/main.py --cli --mode forward --batch-delay 5.0 --max-messages 100
# Clone specific forum topics (e.g. Source Topic ID 7 to Dest Topic ID 12)
python src/telegram_clone/main.py --cli --source-topic 7 --dest-topic 12
💡 How Pause & Resume Works
Whenever you specify a batch limit (e.g. 100), the app forwards 100 files, records their IDs, and stops.
To continue, simply click Start Clone again or re-run the CLI command.
The app automatically loads the database, skips the first 100 messages, and resumes from message #101 seamlessly!
🔒 Safe Use & Anti-Ban Best Practices
Use Plain Forward mode for instant server-side copying.
Keep Anti-Ban Delay at 5.0 seconds or higher when forwarding large volumes of files.
The app automatically detects invalid service messages (like pinned message alerts) and skips them without getting stuck in retry loops.
🌐 GitHub Repository & Contributions
Source code is available on GitHub: [GitHub Repository Link]

Feel free to check out the repo, open issues, or submit PRs! Star ⭐ if you find it helpful!
