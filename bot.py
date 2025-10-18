#!/usr/bin/env python3
import asyncio
import json
import os
from typing import Dict, Any

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from web3 import Web3
from dotenv import load_dotenv
import aiofiles

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEB3_PROVIDER_URI = os.getenv("WEB3_PROVIDER_URI")
CHAIN = os.getenv("CHAIN", "mainnet")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "6"))
DATA_FILE = os.getenv("DATA_FILE", "wallets_db.json")

if not TELEGRAM_TOKEN or not WEB3_PROVIDER_URI:
    raise SystemExit("Please set TELEGRAM_TOKEN and WEB3_PROVIDER_URI in .env")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

w3 = Web3(Web3.WebsocketProvider(WEB3_PROVIDER_URI)) if WEB3_PROVIDER_URI.startswith("ws") else Web3(Web3.HTTPProvider(WEB3_PROVIDER_URI))

# DB layout:
# {
#   "<chat_id>": { "addresses": ["0x.."], "last_checked_block": 17100000 }
# }
db: Dict[str, Any] = {}

async def load_db():
    global db
    if os.path.exists(DATA_FILE):
        async with aiofiles.open(DATA_FILE, mode="r") as f:
            content = await f.read()
            db = json.loads(content) if content.strip() else {}
    else:
        db = {}

async def save_db():
    async with aiofiles.open(DATA_FILE, mode="w") as f:
        await f.write(json.dumps(db, indent=2, ensure_ascii=False))

def norm_address(a: str) -> str:
    return Web3.toChecksumAddress(a) if Web3.isAddress(a) else a

def make_etherscan_tx_url(txhash: str) -> str:
    prefix = {
        "mainnet": "https://etherscan.io/tx/",
        "goerli": "https://goerli.etherscan.io/tx/",
        "sepolia": "https://sepolia.etherscan.io/tx/"
    }.get(CHAIN, "https://etherscan.io/tx/")
    return prefix + txhash

@dp.message(Command(commands=["start", "help"]))
async def cmd_start(msg: Message):
    await msg.answer(
        "Привет! Я бот для отслеживания транзакций Ethereum.\n\n"
        "Команды:\n"
        "/add <address> — добавить кошелёк\n"
        "/remove <address> — убрать кошелёк\n"
        "/list — показать список\n"
        "/help — помощь\n\n"
        "Я пришлю уведомление, когда появится транзакция с участием любого из добавленных адресов."
    )

@dp.message(Command(commands=["list"]))
async def cmd_list(msg: Message):
    chat_id = str(msg.chat.id)
    addrs = db.get(chat_id, {}).get("addresses", [])
    if not addrs:
        await msg.answer("Список пуст. Добавьте адрес командой /add 0x...")
        return
    out = "Отслеживаемые адреса:\n" + "\n".join(addrs)
    await msg.answer(out)

@dp.message(Command(commands=["add"]))
async def cmd_add(msg: Message):
    text = msg.text or ""
    parts = text.split()
    if len(parts) < 2:
        await msg.answer("Использование: /add 0xВашАдрес")
        return
    addr = parts[1].strip()
    try:
        addr = norm_address(addr)
    except Exception:
        await msg.answer("Адрес невалидный. Укажите корректный Ethereum-адрес (0x...).")
        return
    chat_id = str(msg.chat.id)
    if chat_id not in db:
        db[chat_id] = {"addresses": [], "last_checked_block": None}
    if addr in db[chat_id]["addresses"]:
        await msg.answer(f"{addr} уже в списке.")
        return
    db[chat_id]["addresses"].append(addr)
    await save_db()
    await msg.answer(f"Добавил {addr} для отслеживания.")

@dp.message(Command(commands=["remove"]))
async def cmd_remove(msg: Message):
    text = msg.text or ""
    parts = text.split()
    if len(parts) < 2:
        await msg.answer("Использование: /remove 0xВашАдрес")
        return
    addr = parts[1].strip()
    try:
        addr = norm_address(addr)
    except Exception:
        await msg.answer("Адрес невалидный.")
        return
    chat_id = str(msg.chat.id)
    if chat_id not in db or addr not in db[chat_id].get("addresses", []):
        await msg.answer("Адрес не найден в списке.")
        return
    db[chat_id]["addresses"].remove(addr)
    await save_db()
    await msg.answer(f"Удалил {addr} из отслеживания.")

async def notify_tx(chat_id: str, tx: dict):
    txhash = tx.get("hash").hex() if hasattr(tx.get("hash"), "hex") else tx.get("hash")
    frm = tx.get("from")
    to = tx.get("to")
    value_wei = int(tx.get("value", 0))
    value_eth = w3.fromWei(value_wei, "ether")
    block = tx.get("blockNumber")
    url = make_etherscan_tx_url(txhash)
    text = (
        f"🔔 <b>Найдена транзакция</b>\n"
        f"Tx: <code>{txhash}</code>\n"
        f"From: <code>{frm}</code>\n"
        f"To: <code>{to}</code>\n"
        f"Value: {value_eth} ETH\n"
        f"Block: {block}\n"
        f"{url}"
    )
    try:
        await bot.send_message(int(chat_id), text, parse_mode="HTML")
    except Exception as e:
        print("Failed to send message:", e)

async def scan_loop():
    await load_db()
    try:
        current_block = w3.eth.block_number
    except Exception as e:
        print("Ошибка обращения к провайдеру:", e)
        return

    for chat_id, info in db.items():
        if info.get("last_checked_block") is None:
            db[chat_id]["last_checked_block"] = current_block - 1
    await save_db()

    while True:
        try:
            latest = w3.eth.block_number
            if latest is None:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            for chat_id, info in db.items():
                last = info.get("last_checked_block", latest - 1)
                if last >= latest:
                    continue
                addresses = set(info.get("addresses", []))
                addresses = set([Web3.toChecksumAddress(a) for a in addresses if Web3.isAddress(a)])
                for blk_num in range(last + 1, latest + 1):
                    try:
                        block = w3.eth.get_block(blk_num, full_transactions=True)
                    except Exception as e:
                        print(f"Ошибка получения блока {blk_num}: {e}")
                        continue
                    txs = block.transactions or []
                    for tx in txs:
                        frm = tx.get("from")
                        to = tx.get("to")
                        try:
                            frm_cs = Web3.toChecksumAddress(frm) if frm else None
                        except Exception:
                            frm_cs = None
                        try:
                            to_cs = Web3.toChecksumAddress(to) if to else None
                        except Exception:
                            to_cs = None
                        if (frm_cs and frm_cs in addresses) or (to_cs and to_cs in addresses):
                            await notify_tx(chat_id, tx)
                db[chat_id]["last_checked_block"] = latest
            await save_db()
        except Exception as e:
            print("Ошибка в scan_loop:", e)
        await asyncio.sleep(POLL_INTERVAL)

async def main():
    asyncio.create_task(scan_loop())
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
