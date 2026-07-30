import asyncio
import os
import threading
import datetime
from flask import Flask
import discord
from discord import app_commands
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# 1. Flask 백그라운드 웹 서버 (UptimeRobot 5분 피핑용)
# ---------------------------------------------------------------------------
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "WOS Discord Bot is Active and Running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

# ---------------------------------------------------------------------------
# 2. 전역 변수 및 디스코드 / 구글 시트 세팅
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "WOS_Coupon_DB")
GOOGLE_JSON_PATH = os.environ.get("GOOGLE_JSON", "google_key.json")

# 🛑 대량 교환 강제 중단 스위치
cancel_mass_redeem = False

# 디스코드 봇 인텐트 설정
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 구글 시트 연동
sheet_users = None
sheet_codes = None

try:
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_JSON_PATH, scope)
    client = gspread.authorize(creds)
    
    doc = client.open(SPREADSHEET_NAME)
    sheet_users = doc.worksheet("users")
    
    try:
        sheet_codes = doc.worksheet("used_codes")
    except gspread.exceptions.WorksheetNotFound:
        sheet_codes = doc.add_worksheet(title="used_codes", rows="1000", cols="2")
        sheet_codes.append_row(["code", "used_at"])
        
    print("✅ Successfully connected to Google Sheets DB!")
except Exception as e:
    print(f"❌ Failed to connect to Google Sheets DB: {e}")

# ---------------------------------------------------------------------------
# 3. Playwright 핵심 교환 로직 (Render 최적화형)
# ---------------------------------------------------------------------------
async def execute_redeem_with_page(page, uid: str, server: int, gift_code: str, max_retries: int = 2) -> bool:
    """wait_for 타임아웃 오류를 방지하고 안정성을 극대화한 자동화 함수"""
    for attempt in range(1, max_retries + 1):
        try:
            # 1. 쿠폰 웹사이트 접속
            await page.goto("https://wos-giftcode.centurygame.com/", timeout=15000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)

            # 2. Player ID 입력
            uid_input = page.locator("input[placeholder='Player ID'], input[placeholder='플레이어 ID'], input[placeholder*='ID']").first
            await uid_input.fill(str(uid), timeout=10000)

            # 3. State (서버) 입력
            server_input = page.locator("input[placeholder='State'], input[placeholder='왕국'], input[placeholder*='서버'], input[placeholder*='Server']").first
            await server_input.fill(str(server), timeout=10000)

            # 4. Gift Code 입력
            code_input = page.locator("input[placeholder='Enter Gift Code'], input[placeholder='교환 코드를 입력해 주세요'], input[placeholder*='Code'], input[placeholder*='코드']").first
            await code_input.fill(str(gift_code), timeout=10000)

            await page.wait_for_timeout(300)

            # 5. 교환 버튼 클릭
            exchange_btn = page.locator("div.exchange_btn").first
            await exchange_btn.click(timeout=10000)

            # 6. 결과 팝업 대기
            await page.wait_for_timeout(2500)

            # 7. 팝업 메시지 분석
            msg_element = page.locator("p.msg, div.modal_content").first
            popup_text = ""
            if await msg_element.count() > 0:
                popup_text = await msg_element.text_content()
            else:
                popup_text = await page.content()

            popup_text_clean = popup_text.strip()
            popup_text_upper = popup_text_clean.upper()
            print(f"🔍 [UID: {uid}] Popup Text: {popup_text_clean}")

            # 🎯 팝업 결과 정밀 판정
            if any(k in popup_text or k in popup_text_upper for k in ["REDEEMED", "CLAIM THE REWARDS IN YOUR MAIL", "교환 성공", "우편에서 보상", "보상을 확인하세요", "SUCCESS", "CONGRATULATIONS"]):
                print(f"✅ [UID: {uid} / State: {server}] Redeem Success!")
                return True
            elif any(k in popup_text or k in popup_text_upper for k in ["ALREADY CLAIMED", "UNABLE TO CLAIM AGAIN", "이미 수령", "다시 수령", "RECEIVED", "USED"]):
                print(f"ℹ️ [UID: {uid}] Already claimed code. (Marked as Success)")
                return True
            elif any(k in popup_text or k in popup_text_upper for k in ["GIFT CODE NOT FOUND", "CHARACTER INFO IS INCORRECT", "CASE-SENSITIVE", "존재하지 않습니다", "대소문자", "시간이 초과", "만료", "EXPIRED", "INVALID"]):
                print(f"❌ [UID: {uid}] Invalid gift code or incorrect user/state info.")
                return False
            else:
                print(f"⚠️ [Attempt {attempt}/{max_retries}] Unknown popup message (UID: {uid})")

        except Exception as e:
            print(f"❌ [Attempt {attempt}/{max_retries}] Error occurred (UID: {uid}): {e}")

        if attempt < max_retries:
            await asyncio.sleep(1)

    return False

# ---------------------------------------------------------------------------
# 4. 병렬 큐(Queue) 처리 및 실시간 진행률 로직
# ---------------------------------------------------------------------------
async def process_mass_redeem(gift_code: str, interaction: discord.Interaction):
    global cancel_mass_redeem
    cancel_mass_redeem = False

    if not sheet_users:
        await interaction.followup.send("❌ Google Sheet DB가 연결되어 있지 않습니다.", ephemeral=True)
        return

    raw_data = sheet_users.get_all_records()
    users = []
    for row in raw_data:
        row_lower = {str(k).lower().strip(): v for k, v in row.items()}
        uid = str(row_lower.get("uid", "")).strip()
        server = str(row_lower.get("server", "")).strip()
        if uid and server:
            users.append({"uid": uid, "server": server})

    if not users:
        await interaction.followup.send("❌ 등록된 유저 정보(UID, Server)가 없습니다.", ephemeral=True)
        return

    total_count = len(users)

    progress_msg = await interaction.channel.send(
        f"🚀 **쿠폰 대량 교환 시작!** (코드: `{gift_code}`)\n"
        f"📊 **진행률**: `0 / {total_count}`명 (0%) | ⏳ 처리 중..."
    )

    queue = asyncio.Queue()
    for u in users:
        await queue.put(u)

    success_count = 0
    fail_count = 0
    processed_count = 0
    lock = asyncio.Lock()

    CONCURRENCY_LIMIT = 2

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        context = await browser.new_context()

        async def worker(worker_id, page):
            nonlocal success_count, fail_count, processed_count
            global cancel_mass_redeem

            while not queue.empty():
                if cancel_mass_redeem:
                    print(f"🛑 [Worker {worker_id}] 강제 중단 요청 감지.")
                    break

                user = await queue.get()
                uid = user["uid"]
                server = user["server"]

                res = await execute_redeem_with_page(page, uid, server, gift_code)

                async with lock:
                    processed_count += 1
                    if res:
                        success_count += 1
                    else:
                        fail_count += 1

                    if processed_count % 5 == 0 or processed_count == total_count:
                        percentage = round((processed_count / total_count) * 100)
                        try:
                            await progress_msg.edit(
                                content=(
                                    f"🚀 **쿠폰 대량 교환 진행 중...** (코드: `{gift_code}`)\n"
                                    f"📊 **진행률**: `{processed_count} / {total_count}`명 ({percentage}%)\n"
                                    f"✅ 성공: `{success_count}`건 | ❌ 실패: `{fail_count}`건"
                                )
                            )
                        except Exception as e:
                            print(f"⚠️ 진행률 메시지 수정 실패: {e}")

                queue.task_done()

        pages = [await context.new_page() for _ in range(CONCURRENCY_LIMIT)]
        tasks = [asyncio.create_task(worker(i + 1, pages[i])) for i in range(CONCURRENCY_LIMIT)]

        await asyncio.gather(*tasks)
        await browser.close()

    if cancel_mass_redeem:
        await progress_msg.edit(
            content=(
                f"🛑 **쿠폰 교환 작업이 중간에 중단되었습니다.**\n"
                f"📊 **최종 진행 결과**: `{processed_count} / {total_count}`명 처리됨\n"
                f"✅ 성공: `{success_count}`건 | ❌ 실패: `{fail_count}`건"
            )
        )
    else:
        await progress_msg.edit(
            content=(
                f"🎉 **쿠폰 대량 교환 완료!**\n"
                f"- 입력 코드: `{gift_code}`\n"
                f"- 총 계정: `{total_count}`명\n"
                f"- ✅ 성공: `{success_count}`명 / ❌ 실패: `{fail_count}`명"
            )
        )

        if sheet_codes:
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sheet_codes.append_row([gift_code, now_str])

# ---------------------------------------------------------------------------
# 5. 디스코드 이벤트 및 슬래시 커맨드 모음 (wr_ 접두사 적용)
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"🤖 Bot is logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"⚡ Synced {len(synced)} command(s).")
    except Exception as e:
        print(f"❌ Command Sync Error: {e}")

# 1) /wr_register 커맨드
@bot.tree.command(name="wr_register", description="WOS 계정(UID 및 State 번호)을 등록하거나 수정합니다.")
@app_commands.describe(uid="플레이어 ID (숫자)", server="왕국 / State 번호 (숫자)")
async def register(interaction: discord.Interaction, uid: str, server: int):
    if not sheet_users:
        await interaction.response.send_message("❌ 구글 시트 DB가 연동되지 않았습니다.", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    username = interaction.user.display_name
    uid_str = str(uid).strip()

    guild_name = interaction.guild.name if interaction.guild else "Direct Message"
    raw_users = sheet_users.get_all_values()

    for row_idx, row in enumerate(raw_users[1:], start=2):
        if len(row) >= 3 and str(row[2]).strip() == uid_str:
            sheet_users.update_cell(row_idx, 1, discord_id)
            sheet_users.update_cell(row_idx, 2, username)
            sheet_users.update_cell(row_idx, 4, str(server))
            sheet_users.update_cell(row_idx, 5, guild_name)
            
            await interaction.response.send_message(
                f"🔄 **{username}**님의 UID `{uid_str}` 정보가 수정되었습니다. (State: `{server}`, Server: **{guild_name}**)",
                ephemeral=True
            )
            return

    sheet_users.append_row([discord_id, username, uid_str, str(server), guild_name])

    embed = discord.Embed(
        title="✅ 계정 등록 완료",
        description=f"{interaction.user.mention} (**{username}**)님의 정보가 성공적으로 등록되었습니다.",
        color=0x3498DB
    )
    embed.add_field(name="UID", value=uid_str, inline=True)
    embed.add_field(name="State", value=f"#{server}", inline=True)
    embed.add_field(name="Server (From)", value=guild_name, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# 2) /wr_sendcoupon 커맨드
@bot.tree.command(name="wr_sendcoupon", description="모든 등록된 계정에 쿠폰 코드를 교환합니다.")
@app_commands.describe(code="교환할 쿠폰 코드")
async def send_coupon(interaction: discord.Interaction, code: str):
    gift_code = code.strip()

    if sheet_codes:
        used_records = sheet_codes.get_all_records()
        used_list = [str(r.get("code", "")).strip().upper() for r in used_records]
        if gift_code.upper() in used_list:
            await interaction.response.send_message(
                f"⚠️ **이미 사용 처리된 쿠폰 코드입니다!** (`{gift_code}`)\n중복 발송을 방지하기 위해 작업을 실행하지 않습니다.",
                ephemeral=True
            )
            return

    await interaction.response.defer(ephemeral=True)
    asyncio.create_task(process_mass_redeem(gift_code, interaction))

# 3) /wr_history 커맨드
@bot.tree.command(name="wr_history", description="지금까지 발송 완료된 쿠폰 코드 목록을 조회합니다.")
async def history(interaction: discord.Interaction):
    if not sheet_codes:
        await interaction.response.send_message("❌ 사용 내역 DB에 접근할 수 없습니다.", ephemeral=True)
        return

    records = sheet_codes.get_all_records()
    if not records:
        await interaction.response.send_message("📜 지금까지 발송된 쿠폰 내역이 없습니다.", ephemeral=True)
        return

    embed = discord.Embed(title="📜 쿠폰 발송 히스토리", color=0x2ECC71)
    
    recent_records = records[-10:]
    recent_records.reverse()

    for r in recent_records:
        code = r.get("code", "Unknown")
        used_at = r.get("used_at", "Unknown Date")
        embed.add_field(name=f"🎟️ {code}", value=f"🕒 발송 일시: {used_at}", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# 4) /wr_delete_user 커맨드 (관리자용)
@bot.tree.command(name="wr_delete_user", description="[관리자] 특정 UID의 계정 정보를 시트에서 삭제합니다.")
@app_commands.describe(uid="삭제할 플레이어 UID")
@app_commands.checks.has_permissions(administrator=True)
async def delete_user(interaction: discord.Interaction, uid: str):
    if not sheet_users:
        await interaction.response.send_message("❌ 구글 시트 DB가 연결되어 있지 않습니다.", ephemeral=True)
        return

    target_uid = str(uid).strip()
    raw_users = sheet_users.get_all_values()

    for row_idx, row in enumerate(raw_users[1:], start=2):
        if len(row) >= 3 and str(row[2]).strip() == target_uid:
            sheet_users.delete_rows(row_idx)
            await interaction.response.send_message(f"🗑️ UID `{target_uid}` 계정이 삭제되었습니다.", ephemeral=True)
            return

    await interaction.response.send_message(f"❌ UID `{target_uid}` 항목을 찾을 수 없습니다.", ephemeral=True)

@delete_user.error
async def delete_user_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("🚫 이 커맨드는 디스코드 **관리자(Administrator)** 권한이 필요합니다.", ephemeral=True)

# 5) /wr_clear_history 커맨드 (관리자용)
@bot.tree.command(name="wr_clear_history", description="[관리자] 발송된 쿠폰 내역(used_codes)을 모두 초기화합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def clear_history(interaction: discord.Interaction):
    if not sheet_codes:
        await interaction.response.send_message("❌ 시트 DB가 연동되지 않았습니다.", ephemeral=True)
        return

    sheet_codes.clear()
    sheet_codes.append_row(["code", "used_at"])
    await interaction.response.send_message("🧹 **쿠폰 발송 히스토리가 성공적으로 초기화되었습니다!**", ephemeral=True)

@clear_history.error
async def clear_history_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("🚫 이 커맨드는 디스코드 **관리자(Administrator)** 권한이 필요합니다.", ephemeral=True)

# 6) /wr_stop 커맨드
@bot.tree.command(name="wr_stop", description="진행 중인 쿠폰 교환 작업을 강제 중단합니다.")
async def stop_redeem(interaction: discord.Interaction):
    global cancel_mass_redeem
    cancel_mass_redeem = True
    await interaction.response.send_message("🛑 **작업 강제 중단이 요청되었습니다.** 현재 처리 중인 계정까지만 완료 후 멈춥니다.", ephemeral=True)

# ---------------------------------------------------------------------------
# 6. 봇 구동
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if BOT_TOKEN:
        bot.run(BOT_TOKEN)
    else:
        print("❌ BOT_TOKEN 환경 변수가 설정되어 있지 않습니다.")import asyncio
import os
import threading
import datetime
from flask import Flask
import discord
from discord import app_commands
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# 1. Flask 백그라운드 웹 서버 (UptimeRobot 5분 피핑용)
# ---------------------------------------------------------------------------
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "WOS Discord Bot is Active and Running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

# ---------------------------------------------------------------------------
# 2. 전역 변수 및 디스코드 / 구글 시트 세팅
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "WOS_Coupon_DB")
GOOGLE_JSON_PATH = os.environ.get("GOOGLE_JSON", "google_key.json")

# 🛑 대량 교환 강제 중단 스위치
cancel_mass_redeem = False

# 디스코드 봇 인텐트 설정
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 구글 시트 연동
sheet_users = None
sheet_codes = None

try:
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_JSON_PATH, scope)
    client = gspread.authorize(creds)
    
    doc = client.open(SPREADSHEET_NAME)
    sheet_users = doc.worksheet("users")
    
    try:
        sheet_codes = doc.worksheet("used_codes")
    except gspread.exceptions.WorksheetNotFound:
        sheet_codes = doc.add_worksheet(title="used_codes", rows="1000", cols="2")
        sheet_codes.append_row(["code", "used_at"])
        
    print("✅ Successfully connected to Google Sheets DB!")
except Exception as e:
    print(f"❌ Failed to connect to Google Sheets DB: {e}")

# ---------------------------------------------------------------------------
# 3. Playwright 핵심 교환 로직 (Render 최적화형)
# ---------------------------------------------------------------------------
async def execute_redeem_with_page(page, uid: str, server: int, gift_code: str, max_retries: int = 2) -> bool:
    """wait_for 타임아웃 오류를 방지하고 안정성을 극대화한 자동화 함수"""
    for attempt in range(1, max_retries + 1):
        try:
            # 1. 쿠폰 웹사이트 접속
            await page.goto("https://wos-giftcode.centurygame.com/", timeout=15000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)

            # 2. Player ID 입력
            uid_input = page.locator("input[placeholder='Player ID'], input[placeholder='플레이어 ID'], input[placeholder*='ID']").first
            await uid_input.fill(str(uid), timeout=10000)

            # 3. State (서버) 입력
            server_input = page.locator("input[placeholder='State'], input[placeholder='왕국'], input[placeholder*='서버'], input[placeholder*='Server']").first
            await server_input.fill(str(server), timeout=10000)

            # 4. Gift Code 입력
            code_input = page.locator("input[placeholder='Enter Gift Code'], input[placeholder='교환 코드를 입력해 주세요'], input[placeholder*='Code'], input[placeholder*='코드']").first
            await code_input.fill(str(gift_code), timeout=10000)

            await page.wait_for_timeout(300)

            # 5. 교환 버튼 클릭
            exchange_btn = page.locator("div.exchange_btn").first
            await exchange_btn.click(timeout=10000)

            # 6. 결과 팝업 대기
            await page.wait_for_timeout(2500)

            # 7. 팝업 메시지 분석
            msg_element = page.locator("p.msg, div.modal_content").first
            popup_text = ""
            if await msg_element.count() > 0:
                popup_text = await msg_element.text_content()
            else:
                popup_text = await page.content()

            popup_text_clean = popup_text.strip()
            popup_text_upper = popup_text_clean.upper()
            print(f"🔍 [UID: {uid}] Popup Text: {popup_text_clean}")

            # 🎯 팝업 결과 정밀 판정
            if any(k in popup_text or k in popup_text_upper for k in ["REDEEMED", "CLAIM THE REWARDS IN YOUR MAIL", "교환 성공", "우편에서 보상", "보상을 확인하세요", "SUCCESS", "CONGRATULATIONS"]):
                print(f"✅ [UID: {uid} / State: {server}] Redeem Success!")
                return True
            elif any(k in popup_text or k in popup_text_upper for k in ["ALREADY CLAIMED", "UNABLE TO CLAIM AGAIN", "이미 수령", "다시 수령", "RECEIVED", "USED"]):
                print(f"ℹ️ [UID: {uid}] Already claimed code. (Marked as Success)")
                return True
            elif any(k in popup_text or k in popup_text_upper for k in ["GIFT CODE NOT FOUND", "CHARACTER INFO IS INCORRECT", "CASE-SENSITIVE", "존재하지 않습니다", "대소문자", "시간이 초과", "만료", "EXPIRED", "INVALID"]):
                print(f"❌ [UID: {uid}] Invalid gift code or incorrect user/state info.")
                return False
            else:
                print(f"⚠️ [Attempt {attempt}/{max_retries}] Unknown popup message (UID: {uid})")

        except Exception as e:
            print(f"❌ [Attempt {attempt}/{max_retries}] Error occurred (UID: {uid}): {e}")

        if attempt < max_retries:
            await asyncio.sleep(1)

    return False

# ---------------------------------------------------------------------------
# 4. 병렬 큐(Queue) 처리 및 실시간 진행률 로직
# ---------------------------------------------------------------------------
async def process_mass_redeem(gift_code: str, interaction: discord.Interaction):
    global cancel_mass_redeem
    cancel_mass_redeem = False

    if not sheet_users:
        await interaction.followup.send("❌ Google Sheet DB가 연결되어 있지 않습니다.", ephemeral=True)
        return

    raw_data = sheet_users.get_all_records()
    users = []
    for row in raw_data:
        row_lower = {str(k).lower().strip(): v for k, v in row.items()}
        uid = str(row_lower.get("uid", "")).strip()
        server = str(row_lower.get("server", "")).strip()
        if uid and server:
            users.append({"uid": uid, "server": server})

    if not users:
        await interaction.followup.send("❌ 등록된 유저 정보(UID, Server)가 없습니다.", ephemeral=True)
        return

    total_count = len(users)

    progress_msg = await interaction.channel.send(
        f"🚀 **쿠폰 대량 교환 시작!** (코드: `{gift_code}`)\n"
        f"📊 **진행률**: `0 / {total_count}`명 (0%) | ⏳ 처리 중..."
    )

    queue = asyncio.Queue()
    for u in users:
        await queue.put(u)

    success_count = 0
    fail_count = 0
    processed_count = 0
    lock = asyncio.Lock()

    CONCURRENCY_LIMIT = 2

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        context = await browser.new_context()

        async def worker(worker_id, page):
            nonlocal success_count, fail_count, processed_count
            global cancel_mass_redeem

            while not queue.empty():
                if cancel_mass_redeem:
                    print(f"🛑 [Worker {worker_id}] 강제 중단 요청 감지.")
                    break

                user = await queue.get()
                uid = user["uid"]
                server = user["server"]

                res = await execute_redeem_with_page(page, uid, server, gift_code)

                async with lock:
                    processed_count += 1
                    if res:
                        success_count += 1
                    else:
                        fail_count += 1

                    if processed_count % 5 == 0 or processed_count == total_count:
                        percentage = round((processed_count / total_count) * 100)
                        try:
                            await progress_msg.edit(
                                content=(
                                    f"🚀 **쿠폰 대량 교환 진행 중...** (코드: `{gift_code}`)\n"
                                    f"📊 **진행률**: `{processed_count} / {total_count}`명 ({percentage}%)\n"
                                    f"✅ 성공: `{success_count}`건 | ❌ 실패: `{fail_count}`건"
                                )
                            )
                        except Exception as e:
                            print(f"⚠️ 진행률 메시지 수정 실패: {e}")

                queue.task_done()

        pages = [await context.new_page() for _ in range(CONCURRENCY_LIMIT)]
        tasks = [asyncio.create_task(worker(i + 1, pages[i])) for i in range(CONCURRENCY_LIMIT)]

        await asyncio.gather(*tasks)
        await browser.close()

    if cancel_mass_redeem:
        await progress_msg.edit(
            content=(
                f"🛑 **쿠폰 교환 작업이 중간에 중단되었습니다.**\n"
                f"📊 **최종 진행 결과**: `{processed_count} / {total_count}`명 처리됨\n"
                f"✅ 성공: `{success_count}`건 | ❌ 실패: `{fail_count}`건"
            )
        )
    else:
        await progress_msg.edit(
            content=(
                f"🎉 **쿠폰 대량 교환 완료!**\n"
                f"- 입력 코드: `{gift_code}`\n"
                f"- 총 계정: `{total_count}`명\n"
                f"- ✅ 성공: `{success_count}`명 / ❌ 실패: `{fail_count}`명"
            )
        )

        if sheet_codes:
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sheet_codes.append_row([gift_code, now_str])

# ---------------------------------------------------------------------------
# 5. 디스코드 이벤트 및 슬래시 커맨드 모음 (wr_ 접두사 적용)
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"🤖 Bot is logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"⚡ Synced {len(synced)} command(s).")
    except Exception as e:
        print(f"❌ Command Sync Error: {e}")

# 1) /wr_register 커맨드
@bot.tree.command(name="wr_register", description="WOS 계정(UID 및 State 번호)을 등록하거나 수정합니다.")
@app_commands.describe(uid="플레이어 ID (숫자)", server="왕국 / State 번호 (숫자)")
async def register(interaction: discord.Interaction, uid: str, server: int):
    if not sheet_users:
        await interaction.response.send_message("❌ 구글 시트 DB가 연동되지 않았습니다.", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    username = interaction.user.display_name
    uid_str = str(uid).strip()

    guild_name = interaction.guild.name if interaction.guild else "Direct Message"
    raw_users = sheet_users.get_all_values()

    for row_idx, row in enumerate(raw_users[1:], start=2):
        if len(row) >= 3 and str(row[2]).strip() == uid_str:
            sheet_users.update_cell(row_idx, 1, discord_id)
            sheet_users.update_cell(row_idx, 2, username)
            sheet_users.update_cell(row_idx, 4, str(server))
            sheet_users.update_cell(row_idx, 5, guild_name)
            
            await interaction.response.send_message(
                f"🔄 **{username}**님의 UID `{uid_str}` 정보가 수정되었습니다. (State: `{server}`, Server: **{guild_name}**)",
                ephemeral=True
            )
            return

    sheet_users.append_row([discord_id, username, uid_str, str(server), guild_name])

    embed = discord.Embed(
        title="✅ 계정 등록 완료",
        description=f"{interaction.user.mention} (**{username}**)님의 정보가 성공적으로 등록되었습니다.",
        color=0x3498DB
    )
    embed.add_field(name="UID", value=uid_str, inline=True)
    embed.add_field(name="State", value=f"#{server}", inline=True)
    embed.add_field(name="Server (From)", value=guild_name, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# 2) /wr_sendcoupon 커맨드
@bot.tree.command(name="wr_sendcoupon", description="모든 등록된 계정에 쿠폰 코드를 교환합니다.")
@app_commands.describe(code="교환할 쿠폰 코드")
async def send_coupon(interaction: discord.Interaction, code: str):
    gift_code = code.strip()

    if sheet_codes:
        used_records = sheet_codes.get_all_records()
        used_list = [str(r.get("code", "")).strip().upper() for r in used_records]
        if gift_code.upper() in used_list:
            await interaction.response.send_message(
                f"⚠️ **이미 사용 처리된 쿠폰 코드입니다!** (`{gift_code}`)\n중복 발송을 방지하기 위해 작업을 실행하지 않습니다.",
                ephemeral=True
            )
            return

    await interaction.response.defer(ephemeral=True)
    asyncio.create_task(process_mass_redeem(gift_code, interaction))

# 3) /wr_history 커맨드
@bot.tree.command(name="wr_history", description="지금까지 발송 완료된 쿠폰 코드 목록을 조회합니다.")
async def history(interaction: discord.Interaction):
    if not sheet_codes:
        await interaction.response.send_message("❌ 사용 내역 DB에 접근할 수 없습니다.", ephemeral=True)
        return

    records = sheet_codes.get_all_records()
    if not records:
        await interaction.response.send_message("📜 지금까지 발송된 쿠폰 내역이 없습니다.", ephemeral=True)
        return

    embed = discord.Embed(title="📜 쿠폰 발송 히스토리", color=0x2ECC71)
    
    recent_records = records[-10:]
    recent_records.reverse()

    for r in recent_records:
        code = r.get("code", "Unknown")
        used_at = r.get("used_at", "Unknown Date")
        embed.add_field(name=f"🎟️ {code}", value=f"🕒 발송 일시: {used_at}", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# 4) /wr_delete_user 커맨드 (관리자용)
@bot.tree.command(name="wr_delete_user", description="[관리자] 특정 UID의 계정 정보를 시트에서 삭제합니다.")
@app_commands.describe(uid="삭제할 플레이어 UID")
@app_commands.checks.has_permissions(administrator=True)
async def delete_user(interaction: discord.Interaction, uid: str):
    if not sheet_users:
        await interaction.response.send_message("❌ 구글 시트 DB가 연결되어 있지 않습니다.", ephemeral=True)
        return

    target_uid = str(uid).strip()
    raw_users = sheet_users.get_all_values()

    for row_idx, row in enumerate(raw_users[1:], start=2):
        if len(row) >= 3 and str(row[2]).strip() == target_uid:
            sheet_users.delete_rows(row_idx)
            await interaction.response.send_message(f"🗑️ UID `{target_uid}` 계정이 삭제되었습니다.", ephemeral=True)
            return

    await interaction.response.send_message(f"❌ UID `{target_uid}` 항목을 찾을 수 없습니다.", ephemeral=True)

@delete_user.error
async def delete_user_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("🚫 이 커맨드는 디스코드 **관리자(Administrator)** 권한이 필요합니다.", ephemeral=True)

# 5) /wr_clear_history 커맨드 (관리자용)
@bot.tree.command(name="wr_clear_history", description="[관리자] 발송된 쿠폰 내역(used_codes)을 모두 초기화합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def clear_history(interaction: discord.Interaction):
    if not sheet_codes:
        await interaction.response.send_message("❌ 시트 DB가 연동되지 않았습니다.", ephemeral=True)
        return

    sheet_codes.clear()
    sheet_codes.append_row(["code", "used_at"])
    await interaction.response.send_message("🧹 **쿠폰 발송 히스토리가 성공적으로 초기화되었습니다!**", ephemeral=True)

@clear_history.error
async def clear_history_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("🚫 이 커맨드는 디스코드 **관리자(Administrator)** 권한이 필요합니다.", ephemeral=True)

# 6) /wr_stop 커맨드
@bot.tree.command(name="wr_stop", description="진행 중인 쿠폰 교환 작업을 강제 중단합니다.")
async def stop_redeem(interaction: discord.Interaction):
    global cancel_mass_redeem
    cancel_mass_redeem = True
    await interaction.response.send_message("🛑 **작업 강제 중단이 요청되었습니다.** 현재 처리 중인 계정까지만 완료 후 멈춥니다.", ephemeral=True)

# ---------------------------------------------------------------------------
# 6. 봇 구동
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if BOT_TOKEN:
        bot.run(BOT_TOKEN)
    else:
        print("❌ BOT_TOKEN 환경 변수가 설정되어 있지 않습니다.")