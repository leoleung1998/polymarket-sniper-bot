"""
Telegram Control Bot — Claude-powered remote control for Polymarket Bot.

Send messages from your phone, Claude reads logs/code, makes fixes, deploys.
Runs as a separate systemd service on the same EC2 server.

Commands:
  /status  — Bot status, P&L, recent trades
  /logs    — Last 20 log lines
  /restart — Restart the trading bot
  /pause   — Stop the trading bot
  /resume  — Start the trading bot
  /help    — Show available commands

Or just send natural language:
  "why aren't we getting any fills?"
  "increase the edge threshold to 12%"
  "show me the last 5 trades"
  "what's the weather forecast for Dallas?"

Claude has access to read files, edit code, check logs, and restart the bot.
"""

import ssl_patch  # noqa: F401 — must be first, patches SSL for ProtonVPN
import os
import json
import subprocess
import time
import ssl
import urllib.request
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

BOT_DIR = Path(__file__).parent.resolve()
POLL_INTERVAL = 2  # seconds between Telegram update checks
MAX_MESSAGE_LENGTH = 4000  # Telegram max is 4096

# ── Telegram API helpers ──

def tg_request(method: str, data: dict = None) -> dict:
    """Make a Telegram Bot API request."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    if data:
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
        return json.loads(resp.read().decode())


def send_message(chat_id: str, text: str):
    """Send a message, splitting if too long."""
    # Split long messages
    chunks = []
    while len(text) > MAX_MESSAGE_LENGTH:
        # Find a good split point
        split_at = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            split_at = MAX_MESSAGE_LENGTH
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    chunks.append(text)

    for chunk in chunks:
        if chunk.strip():
            tg_request("sendMessage", {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            })


def send_typing(chat_id: str):
    """Show typing indicator."""
    try:
        tg_request("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


# ── Tool implementations (what Claude can do) ──

# Script names for each "service" locally
LOCAL_SCRIPTS = {
    "polymarket-bot": "bot.py",
    "crypto-maker": "arb_engine_v5_maker.py",
}
LOG_FILE = BOT_DIR / "bot.log"


def _find_pid(script_name: str) -> str | None:
    """Return PID of a running python script, or None.
    Checks the script name itself AND bot.py (which runs all engines in dual mode)."""
    for name in [script_name, "bot.py"]:
        result = subprocess.run(
            ["pgrep", "-f", name],
            capture_output=True, text=True,
        )
        pids = result.stdout.strip().splitlines()
        if pids:
            return pids[0]
    return None


def tool_read_logs(lines: int = 30, service: str = "polymarket-bot") -> str:
    """Read recent bot logs from local log file and data directory."""
    try:
        output = []
        if LOG_FILE.exists():
            all_lines = LOG_FILE.read_text().splitlines()
            output.append("\n".join(all_lines[-lines:]) or "Log file is empty")

        data_dir = BOT_DIR / "data"
        if data_dir.exists():
            log_files = sorted(data_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
            for lf in log_files[:3]:
                all_lines = lf.read_text().splitlines()
                if all_lines:
                    output.append(f"\n--- {lf.name} ---\n" + "\n".join(all_lines[-lines:]))

        if output:
            return "\n".join(output)
        return f"No log files found in {BOT_DIR} or {BOT_DIR / 'data'}"
    except Exception as e:
        return f"Error reading logs: {e}"


def _get_bot_command(pid: str) -> str:
    """Return the subcommand bot.py was launched with (tp, dual, bracket, maker, run, etc.)"""
    try:
        result = subprocess.run(["ps", "-p", pid, "-o", "args="], capture_output=True, text=True)
        args = result.stdout.strip()
        for cmd in ("dual", "bracket", "maker", "tp", "run", "scan"):
            if cmd in args:
                return cmd
        return "unknown"
    except Exception:
        return "unknown"


def tool_bot_status() -> str:
    """Get bot status by checking running processes."""
    lines = []
    pid = _find_pid("bot.py")
    if pid:
        mem_result = subprocess.run(["ps", "-o", "rss=", "-p", pid], capture_output=True, text=True)
        try:
            mem_mb = int(mem_result.stdout.strip()) / 1024
            mem_str = f"{mem_mb:.0f}MB"
        except (ValueError, TypeError):
            mem_str = "?"
        cmd = _get_bot_command(pid)
        strategy_names = {
            "tp": "Sniper + Take Profit",
            "dual": "Weather + Crypto Maker (dual)",
            "bracket": "Weather Bracket Bot",
            "maker": "Crypto Maker Bot",
            "run": "Legacy Sniper (v1)",
            "scan": "Scan only (no trading)",
        }
        label = strategy_names.get(cmd, f"bot.py {cmd}")
        lines.append(f"🟢 {label}: Running (PID {pid}, {mem_str} RAM)")
    else:
        lines.append(f"🔴 Bot: Not running")

    # Show last bankroll line from log if available
    if LOG_FILE.exists():
        log_lines = LOG_FILE.read_text().splitlines()
        for log_line in reversed(log_lines):
            if "Bankroll:" in log_line and "P&L:" in log_line:
                lines.append(f"\n📊 {log_line.strip()}")
                break

    # Fetch live Polymarket wallet balance
    try:
        import ssl_patch
        from trader import init_client
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        client = init_client(
            PRIVATE_KEY,
            signature_type=int(os.getenv("SIGNATURE_TYPE", "2")),
            funder=os.getenv("FUNDER"),
        )
        result = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        balance = int(result.get("balance", 0)) / 1e6
        lines.append(f"💰 Polymarket balance: ${balance:.2f} USDC")
    except Exception as e:
        lines.append(f"💰 Balance: unavailable ({e})")

    return "\n".join(lines) if lines else "Could not get status"


def tool_restart_bot() -> str:
    """Restart the trading bot by killing and relaunching it."""
    try:
        script = LOCAL_SCRIPTS["polymarket-bot"]
        pid = _find_pid(script)
        if pid:
            subprocess.run(["kill", pid], timeout=5)
            time.sleep(2)
        subprocess.Popen(
            ["python", str(BOT_DIR / script)],
            cwd=str(BOT_DIR),
            stdout=open(LOG_FILE, "a"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(2)
        return "✅ Bot restarted!\n\n" + tool_bot_status()
    except Exception as e:
        return f"Error restarting: {e}"


def tool_pause_bot() -> str:
    """Stop the trading bot process."""
    try:
        script = LOCAL_SCRIPTS["polymarket-bot"]
        pid = _find_pid(script)
        if pid:
            subprocess.run(["kill", pid], timeout=5)
            return "⏸️ Bot paused. Send /resume to start again."
        return "Bot is not running."
    except Exception as e:
        return f"Error stopping: {e}"


def tool_resume_bot() -> str:
    """Start the trading bot process."""
    try:
        script = LOCAL_SCRIPTS["polymarket-bot"]
        pid = _find_pid(script)
        if pid:
            return f"Bot is already running (PID {pid})."
        subprocess.Popen(
            ["python", str(BOT_DIR / script)],
            cwd=str(BOT_DIR),
            stdout=open(LOG_FILE, "a"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(2)
        return tool_bot_status()
    except Exception as e:
        return f"Error starting: {e}"


def tool_read_file(file_path: str) -> str:
    """Read a file from the bot directory."""
    try:
        # Security: only allow reading files within the bot directory
        full_path = (BOT_DIR / file_path).resolve()
        if not str(full_path).startswith(str(BOT_DIR)):
            return "Error: Can only read files within the bot directory"
        if not full_path.exists():
            return f"File not found: {file_path}"
        content = full_path.read_text()
        if len(content) > 8000:
            return content[:8000] + "\n... (truncated)"
        return content
    except Exception as e:
        return f"Error reading file: {e}"


def tool_edit_file(file_path: str, old_text: str, new_text: str) -> str:
    """Edit a file by replacing old_text with new_text."""
    try:
        full_path = (BOT_DIR / file_path).resolve()
        if not str(full_path).startswith(str(BOT_DIR)):
            return "Error: Can only edit files within the bot directory"
        if not full_path.exists():
            return f"File not found: {file_path}"

        content = full_path.read_text()
        if old_text not in content:
            return f"Error: old_text not found in {file_path}"

        count = content.count(old_text)
        new_content = content.replace(old_text, new_text, 1)
        full_path.write_text(new_content)
        return f"Edited {file_path} ({count} occurrence(s) found, replaced first)"
    except Exception as e:
        return f"Error editing file: {e}"


def tool_run_command(command: str) -> str:
    """Run a shell command (limited to safe operations)."""
    # Block dangerous commands
    dangerous = ["rm -rf", "mkfs", "dd if=", "> /dev/", "shutdown", "reboot",
                 "passwd", "chmod 777", "curl | bash", "wget | bash"]
    for d in dangerous:
        if d in command:
            return f"Blocked dangerous command: {command}"

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=30, cwd=str(BOT_DIR)
        )
        output = result.stdout + result.stderr
        if len(output) > 4000:
            output = output[:4000] + "\n... (truncated)"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Command timed out (30s limit)"
    except Exception as e:
        return f"Error: {e}"


def tool_kill_switch() -> str:
    """Emergency: cancel all open orders and sell all positions."""
    try:
        from take_profit import kill_switch
        from trader import init_client
        client = init_client(
            PRIVATE_KEY,
            signature_type=int(os.getenv("SIGNATURE_TYPE", "2")),
            funder=os.getenv("FUNDER"),
        )
        return kill_switch(client)
    except Exception as e:
        return f"❌ Kill switch error: {e}"


def tool_tp_status() -> str:
    """Show open positions vs TP threshold and recent TP sells."""
    try:
        from take_profit import load_open_positions, get_current_price, TP_THRESHOLD, TP_LOG_FILE
        import json
        lines = [f"💰 *Take Profit Monitor* (threshold: +{TP_THRESHOLD*100:.0f}%)\n"]

        positions = load_open_positions()
        if not positions:
            lines.append("No open positions tracked.")
        else:
            lines.append(f"*Open positions: {len(positions)}*")
            for pos in positions[:10]:
                current = get_current_price(pos["token_id"])
                buy = pos["buy_price"]
                gain = (current - buy) / buy if current and buy > 0 else 0
                bar = "🟢" if gain >= TP_THRESHOLD else "🟡" if gain > 0 else "🔴"
                q = pos.get("question", pos["token_id"])[:40]
                lines.append(f"{bar} {q}: buy=${buy:.3f} now=${current:.3f} ({gain*100:+.0f}%)" if current else f"⚪ {q}: buy=${buy:.3f} (no price)")

        # Recent TP sells
        if TP_LOG_FILE.exists():
            sells = json.loads(TP_LOG_FILE.read_text())
            sells = [s for s in sells if s.get("type") == "sell" and s.get("source") != "test"]
            if sells:
                lines.append(f"\n*Recent TP sells: {len(sells)}*")
                for s in sells[-5:]:
                    lines.append(f"✅ {s.get('question','?')[:35]}: +{s.get('gain_pct',0):.0f}% | P&L ${s.get('pnl',0):+.3f}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching TP status: {e}"


def tool_list_files(directory: str = ".") -> str:
    """List files in a directory."""
    try:
        full_path = (BOT_DIR / directory).resolve()
        if not str(full_path).startswith(str(BOT_DIR)):
            return "Error: Can only list files within the bot directory"
        result = subprocess.run(
            ["ls", "-la", str(full_path)],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout or result.stderr
    except Exception as e:
        return f"Error: {e}"


def tool_deploy_restart() -> str:
    """After editing files, restart the bot to apply changes."""
    return tool_restart_bot()


# ── Claude API with tool use ──

TOOLS = [
    {
        "name": "read_logs",
        "description": "Read recent trading bot logs from systemd journal. Shows scan results, trades, errors.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lines": {"type": "integer", "description": "Number of log lines to read (default 30)", "default": 30}
            }
        }
    },
    {
        "name": "bot_status",
        "description": "Get the trading bot's systemd service status (running/stopped, uptime, memory usage).",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "restart_bot",
        "description": "Restart the trading bot service. Use after making code changes.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "pause_bot",
        "description": "Stop the trading bot. It will not trade until resumed.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "resume_bot",
        "description": "Start/resume the trading bot.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "read_file",
        "description": "Read a source code file from the bot directory. Use to understand how the bot works or diagnose issues.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Relative path from bot directory, e.g. 'arb_engine_v4.py'"}
            },
            "required": ["file_path"]
        }
    },
    {
        "name": "edit_file",
        "description": "Edit a source code file by replacing old_text with new_text. Use to fix bugs, change parameters, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Relative path to file"},
                "old_text": {"type": "string", "description": "Exact text to find and replace"},
                "new_text": {"type": "string", "description": "New text to replace with"}
            },
            "required": ["file_path", "old_text", "new_text"]
        }
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the bot directory. Use for checking Python versions, pip packages, disk space, network, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"}
            },
            "required": ["command"]
        }
    },
    {
        "name": "list_files",
        "description": "List files in a directory within the bot project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Relative directory path (default '.')", "default": "."}
            }
        }
    },
    {
        "name": "deploy_restart",
        "description": "Restart the bot after making code changes to apply them.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "tp_status",
        "description": "Show open positions vs take profit threshold and recent TP sells.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "kill_switch",
        "description": "Emergency kill switch: cancel ALL open orders and sell ALL open positions immediately. Use when user says 'kill', 'exit all', 'close everything', 'emergency stop'.",
        "input_schema": {"type": "object", "properties": {}}
    },
]

TOOL_HANDLERS = {
    "read_logs": lambda args: tool_read_logs(args.get("lines", 30)),
    "bot_status": lambda args: tool_bot_status(),
    "restart_bot": lambda args: tool_restart_bot(),
    "pause_bot": lambda args: tool_pause_bot(),
    "resume_bot": lambda args: tool_resume_bot(),
    "read_file": lambda args: tool_read_file(args["file_path"]),
    "edit_file": lambda args: tool_edit_file(args["file_path"], args["old_text"], args["new_text"]),
    "run_command": lambda args: tool_run_command(args["command"]),
    "list_files": lambda args: tool_list_files(args.get("directory", ".")),
    "deploy_restart": lambda args: tool_deploy_restart(),
    "tp_status": lambda args: tool_tp_status(),
    "kill_switch": lambda args: tool_kill_switch(),
}

SYSTEM_PROMPT = """You are the remote control AI for a Polymarket trading bot running locally on a Mac (not a Linux server).

## Available strategies (python bot.py <command>):
- `bracket` — Weather bracket bot (GFS 31-member ensemble, temperature brackets)
- `maker` — Crypto maker bot (15-min BTC/ETH/SOL up/down markets)
- `dual` — Weather + crypto maker running in parallel
- `tp` — Sniper + Take Profit (buys outcomes $0.005-$0.025, sells at +40% gain)
- `run` — Legacy v1 sniper (no take profit)

## IMPORTANT RULES:
1. For ANY question about what is running, which strategy is active, current config values, or current status — ALWAYS call `read_logs` first. The startup banner in the log shows the exact strategy name and all config values. Never answer these questions from code files — the code contains ALL strategies, not just the running one.
2. Only use `read_file` when asked to diagnose a bug, understand logic, or make a code change — not to answer "what is running" questions.
3. `bot_status` tells you if the process is alive and the PID. `read_logs` tells you what it's actually doing.

## Key files:
- bot.py — CLI entry point. The command used to launch it determines which strategy is active.
- arb_engine_v4.py — Weather bracket engine
- arb_engine_v5_maker.py — Crypto maker engine
- take_profit.py — Take profit engine (monitors positions, sells at TP_THRESHOLD gain)
- data/tp_log.json — Take profit sell history
- data/v4_trades.json — Weather bot trade history
- data/orders.json — Sniper buy history
- bot.log — Live bot output (startup banner shows exact config values in use)

## You can:
1. Check what's running via bot_status + read_logs
2. Use tp_status to see open positions vs TP threshold
3. Read and edit source files for debugging/changes
4. Restart the bot after changes
5. Run shell commands for debugging

Keep responses concise — this is Telegram. Never share API keys, private keys, or secrets.
If a question is outside the scope of the trading bot, say so immediately without using tools."""


def call_claude(user_message: str, chat_id: str) -> str:
    """Call Claude API with tool use, handle tool calls in a loop."""
    messages = [{"role": "user", "content": user_message}]

    for iteration in range(12):  # Max 12 tool-use rounds
        # Call Claude API
        request_body = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 2048,
            "system": SYSTEM_PROMPT,
            "tools": TOOLS,
            "messages": messages,
        }

        body = json.dumps(request_body).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
        )

        try:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=60, context=ssl_ctx) as resp:
                response = json.loads(resp.read().decode())
        except Exception as e:
            return f"Claude API error: {e}"

        # Process response
        stop_reason = response.get("stop_reason", "end_turn")
        content_blocks = response.get("content", [])

        if stop_reason == "tool_use":
            # Extract tool calls and execute them
            tool_results = []
            for block in content_blocks:
                if block.get("type") == "tool_use":
                    tool_name = block["name"]
                    tool_input = block.get("input", {})
                    tool_id = block["id"]

                    # Show typing while executing tool
                    send_typing(chat_id)

                    # Execute the tool
                    handler = TOOL_HANDLERS.get(tool_name)
                    if handler:
                        try:
                            result = handler(tool_input)
                        except Exception as e:
                            result = f"Tool error: {e}"
                    else:
                        result = f"Unknown tool: {tool_name}"

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": str(result),
                    })

            # Add assistant message and tool results to conversation
            messages.append({"role": "assistant", "content": content_blocks})
            messages.append({"role": "user", "content": tool_results})

        else:
            # Final text response
            text_parts = []
            for block in content_blocks:
                if block.get("type") == "text":
                    text_parts.append(block["text"])
            return "\n".join(text_parts) if text_parts else "(no response)"

    return "Reached max tool iterations. Try a simpler request."


# ── Quick command handlers (no Claude needed) ──

def handle_quick_command(command: str) -> str | None:
    """Handle simple commands without calling Claude API."""
    cmd = command.strip().lower()

    if cmd == "/status":
        return tool_bot_status()
    elif cmd == "/logs":
        return tool_read_logs(20)
    elif cmd == "/restart":
        return tool_restart_bot()
    elif cmd == "/pause":
        return tool_pause_bot()
    elif cmd == "/resume":
        return tool_resume_bot()
    elif cmd == "/tp":
        return tool_tp_status()
    elif cmd == "/kill":
        return tool_kill_switch()
    elif cmd == "/help":
        return (
            "*Polymarket Bot Remote Control*\n\n"
            "*Quick commands:*\n"
            "`/status` — Bot status & uptime\n"
            "`/logs` — Recent log lines\n"
            "`/tp` — Take profit positions & sells\n"
            "`/kill` — 🚨 Cancel all orders + sell all positions\n"
            "`/restart` — Restart the bot\n"
            "`/pause` — Stop trading\n"
            "`/resume` — Resume trading\n"
            "`/help` — This message\n\n"
            "*Or ask me anything:*\n"
            "\"why aren't we getting fills?\"\n"
            "\"change edge threshold to 12%\"\n"
            "\"show today's P&L\"\n"
            "\"what's the ensemble forecast for Dallas?\"\n\n"
            "I can read code, edit files, check logs, and restart the bot."
        )

    return None  # Not a quick command — use Claude


# ── Main polling loop ──

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return
    if not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_CHAT_ID not set in .env")
        return
    if not ANTHROPIC_API_KEY:
        print("WARNING: ANTHROPIC_API_KEY not set — AI features disabled, only quick commands work")

    print(f"Telegram Control Bot started")
    print(f"  Chat ID: {TELEGRAM_CHAT_ID}")
    print(f"  Bot dir: {BOT_DIR}")
    print(f"  Claude:  {'enabled' if ANTHROPIC_API_KEY else 'DISABLED (no API key)'}")

    # Send startup message
    send_message(TELEGRAM_CHAT_ID,
        "🤖 *Control Bot Online*\n"
        f"Claude AI: {'enabled' if ANTHROPIC_API_KEY else 'disabled'}\n"
        "Send /help for commands"
    )

    last_update_id = 0

    while True:
        try:
            # Long-poll for new messages
            params = {"offset": last_update_id + 1, "timeout": 30, "allowed_updates": ["message"]}
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url)

            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=35, context=ssl_ctx) as resp:
                updates = json.loads(resp.read().decode())

            if not updates.get("ok"):
                continue

            for update in updates.get("result", []):
                update_id = update["update_id"]
                last_update_id = max(last_update_id, update_id)

                message = update.get("message", {})
                chat_id = str(message.get("chat", {}).get("id", ""))
                text = message.get("text", "").strip()

                # Security: only respond to the configured chat ID
                if chat_id != TELEGRAM_CHAT_ID or not text:
                    continue

                print(f"[telegram] Received: {text[:50]}")

                # Try quick command first
                quick_response = handle_quick_command(text)
                if quick_response is not None:
                    send_message(chat_id, quick_response)
                    continue

                # Use Claude for everything else
                if not ANTHROPIC_API_KEY:
                    send_message(chat_id, "AI features disabled — set ANTHROPIC_API_KEY in .env")
                    continue

                send_typing(chat_id)
                response = call_claude(text, chat_id)
                send_message(chat_id, response)

        except KeyboardInterrupt:
            print("\nShutting down...")
            break
        except Exception as e:
            print(f"[telegram] Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
