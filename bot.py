import asyncio
import json
import os
import re
import sys
import subprocess
from datetime import datetime
from threading import Thread
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from playwright.async_api import async_playwright

# ==========================================
# ⚡ 브라우저 자동 검사/설치
# ==========================================
try:
    print("🌐 Playwright 브라우저 검사 진행...")
    subprocess.run(["python", "-m", "playwright", "install"], check=False)
except Exception as e:
    print(f"⚠️ 브라우저 설치 과정 스킵/경고: {e}")

# ==========================================
# ⚙️ 기본 설정 구역
# ==========================================
sys.stdout.reconfigure(line_buffering=True)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_DISCORD_BOT_TOKEN_HERE")
MONITOR_CHANNEL_ID = 973162050333327390  # 모니터링 채널 ID
REPORT_CHANNEL_ID = 1532160917943484626   # 결과 보고 채널 ID

SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "wos_bot_db")
GOOGLE_JSON_RAW = os.environ.get("GOOGLE_JSON", "")

# 🛑 대량 교환 강제 중단 플래그
cancel_mass_redeem = False

# ==========================================
# 📊 구글 시트 DB 세팅
# ==========================================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

sheet_users = None
sheet_codes = None
sheet_failed = None

try:
    if GOOGLE_JSON_RAW:
        creds_dict = json.loads(GOOGLE_JSON_RAW)
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name("service_account.json", scope)

    gc = gspread.authorize(creds)
    sh = gc.open(SPREADSHEET_NAME)

    sheet_users = sh.sheet1
    
    try:
        sheet_codes = sh.worksheet("used_codes")
    except gspread.exceptions.WorksheetNotFound:
        sheet_codes = sh.add_worksheet(title="used_codes", rows="1000", cols="3")
        sheet_codes.append_row(["code", "result_summary", "used_at"])

    try:
        sheet_failed = sh.worksheet("failed_users")
    except gspread.exceptions.WorksheetNotFound:
        sheet_failed = sh.add_worksheet(title="failed_users", rows="1000", cols="5")
        sheet_failed.append_row(["discord_id", "username", "uid", "server", "from"])

    print("✅ 구글 시트 데이터베이스 연동 성공!")
except Exception as e:
    print(f"❌ [초기화 에러] 구글 시트 연동 실패: {e}")

# ==========================================
# 🌐 Render 유지용 Web Server
# ==========================================
web_app = Flask('')

@web_app.route('/')
def home():
    return "WOS Discord Bot is Active and Running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web)
    t.daemon = True
    t.start()

# ==========================================
# 🤖 디스코드 봇 클래스 정의
# ==========================================
class WOSBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        monitor_coupon_channel.start()
        print("✅ Slash commands synced and monitoring loop started.")

bot = WOSBot()

@bot.event
async def on_ready():
    await bot.change_presence(status=discord.Status.online, activity=discord.Game("WOS Gift Code Redeem"))
    print(f"✅ [{bot.user.name}] Discord online connection ready!")

# ==========================================
# 🔄 Playwright 자동 입력 로직
# ==========================================
async def execute_redeem_with_page(
    page, uid: str, server: int, gift_code: str, max_retries: int = 3
) -> bool:
    """한글/영문 모든 입력창 및 성공/실패/중복 팝업 완벽 대응 자동화"""
    for attempt in range(1, max_retries + 1):
        try:
            if attempt == 1:
                await page.goto(
                    "https://wos-giftcode.centurygame.com/",
                    timeout=20000,
                    wait_until="commit",
                )
            else:
                await page.reload(timeout=20000, wait_until="commit")

            await page.wait_for_timeout(1500)

            uid_input = page.locator(
                "input[placeholder='Player ID'], input[placeholder='플레이어 ID'], input[placeholder*='ID']"
            ).first
            await uid_input.wait_for(state="attached", timeout=15000)
            await uid_input.fill(str(uid), timeout=5000)

            server_input = page.locator(
                "input[placeholder='State'], input[placeholder='왕국'], input[placeholder*='서버'], input[placeholder*='Server']"
            ).first
            await server_input.fill(str(server), timeout=5000)

            code_input = page.locator(
                "input[placeholder='Enter Gift Code'], input[placeholder='교환 코드를 입력해 주세요'], input[placeholder*='Code'], input[placeholder*='코드']"
            ).first
            await code_input.fill(str(gift_code), timeout=5000)

            await page.wait_for_timeout(500)

            exchange_btn = page.locator("div.exchange_btn").first
            await exchange_btn.click(timeout=5000)

            await page.wait_for_timeout(2500)

            msg_element = page.locator("p.msg, div.modal_content").first
            popup_text = ""

            if await msg_element.count() > 0:
                popup_text = await msg_element.text_content()
            else:
                popup_text = await page.content()

            popup_text_clean = popup_text.strip()
            popup_text_upper = popup_text_clean.upper()
            print(f"🔍 [UID: {uid}] Popup Text: {popup_text_clean}")

            if any(
                k in popup_text or k in popup_text_upper
                for k in [
                    "REDEEMED",
                    "CLAIM THE REWARDS IN YOUR MAIL",
                    "교환 성공",
                    "우편에서 보상",
                    "보상을 확인하세요",
                    "SUCCESS",
                    "CONGRATULATIONS",
                ]
            ):
                print(f"✅ [UID: {uid} / State: {server}] Redeem Success!")
                return True

            elif any(
                k in popup_text or k in popup_text_upper
                for k in [
                    "ALREADY CLAIMED",
                    "UNABLE TO CLAIM AGAIN",
                    "이미 수령",
                    "다시 수령",
                    "RECEIVED",
                    "USED",
                ]
            ):
                print(f"ℹ️ [UID: {uid}] Already claimed code. (Marked as Success)")
                return True

            elif any(
                k in popup_text or k in popup_text_upper
                for k in [
                    "GIFT CODE NOT FOUND",
                    "CHARACTER INFO IS INCORRECT",
                    "CASE-SENSITIVE",
                    "존재하지 않습니다",
                    "대소문자",
                    "시간이 초과",
                    "만료",
                    "EXPIRED",
                    "INVALID",
                ]
            ):
                print(f"❌ [UID: {uid}] Invalid gift code or incorrect user/state info.")
                return False

            else:
                print(f"⚠️ [Attempt {attempt}/{max_retries}] Unknown popup message (UID: {uid})")

        except Exception as e:
            print(
                f"❌ [Attempt {attempt}/{max_retries}] Error occurred (UID: {uid}): {e}"
            )

        if attempt < max_retries:
            await asyncio.sleep(1.5)

    return False

# ==========================================
# ⚡ 초고속 병렬 처리 대량 교환 로직
# ==========================================
async def process_mass_redeem(gift_code: str, target_channel):
    global cancel_mass_redeem
    cancel_mass_redeem = False

    if not sheet_users or not sheet_codes:
        if target_channel:
            await target_channel.send("❌ Google Sheet database connection error.")
        return

    gift_code = gift_code.strip()

    try:
        raw_users = sheet_users.get_all_values()
        if len(raw_users) <= 1:
            if target_channel:
                await target_channel.send("❌ No registered users found in the database.")
            return

        headers = [str(h).strip().lower() for h in raw_users[0]]
        users_records = []

        for row in raw_users[1:]:
            if not row or not any(row):
                continue
            user_dict = {}
            for i, h in enumerate(headers):
                if i < len(row):
                    user_dict[h] = str(row[i]).strip()
            user_dict["_raw_row"] = row
            users_records.append(user_dict)

        used_codes_records = sheet_codes.get_all_records()

        if sheet_failed:
            sheet_failed.clear()
            header_row = raw_users[0] if raw_users else ["discord_id", "username", "uid", "server", "from"]
            sheet_failed.append_row(header_row)

    except Exception as e:
        print(f"❌ Failed to load Google Sheet data: {e}")
        if target_channel:
            await target_channel.send(f"❌ Error reading Google Sheet data: {e}")
        return

    if any(str(row.get('code')).strip() == gift_code for row in used_codes_records):
        if target_channel:
            await target_channel.send(f"⚠️ Gift code `{gift_code}` has already been processed.")
        return

    total_users = len(users_records)
    status_msg = None
    if target_channel:
        status_msg = await target_channel.send(f"🚀 **New Gift Code Found!** [`{gift_code}`]\nStarting parallel auto redemption for **{total_users} account(s)**...")

    success_count = 0
    fail_count = 0
    processed_count = 0
    pass1_failed_list = []
    lock = asyncio.Lock()

    CONCURRENCY_LIMIT = 2
    queue = asyncio.Queue()

    for user in users_records:
        await queue.put(user)

    async def worker(context):
        nonlocal success_count, fail_count, processed_count
        global cancel_mass_redeem

        page = await context.new_page()

        while not queue.empty():
            if cancel_mass_redeem:
                print("🛑 강제 중단 요청 감지. 작업을 멈춥니다.")
                break

            try:
                user = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            uid = user.get("uid")
            server = user.get("server")

            if not uid or not server:
                async with lock:
                    fail_count += 1
                    processed_count += 1
                    pass1_failed_list.append(user)
                queue.task_done()
                continue

            try:
                is_success = await execute_redeem_with_page(page, str(uid), server, gift_code, max_retries=3)
            except Exception as e:
                print(f"❌ Exception occurred (UID: {uid}): {e}")
                is_success = False

            async with lock:
                if is_success:
                    success_count += 1
                else:
                    fail_count += 1
                    pass1_failed_list.append(user)

                processed_count += 1

                if status_msg and (processed_count % 5 == 0 or processed_count == total_users):
                    try:
                        await status_msg.edit(
                            content=f"🔄 **Processing Auto Redeem (Pass 1)...** [`{gift_code}`] [{processed_count}/{total_users}]\n✅ Success: {success_count} | ❌ Failed: {fail_count}"
                        )
                    except Exception:
                        pass

            queue.task_done()

        await page.close()

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

            workers = [asyncio.create_task(worker(context)) for _ in range(CONCURRENCY_LIMIT)]
            await asyncio.gather(*workers)

            await browser.close()
    except Exception as pw_err:
        print(f"❌ Playwright Critical Error: {pw_err}")

    # 2차 재시도 로직
    final_failed_list = []

    if pass1_failed_list and not cancel_mass_redeem:
        if status_msg:
            try:
                await status_msg.edit(
                    content=f"🔄 1차 교환 완료! (성공: {success_count} / 실패: {fail_count})\n"
                            f"⏳ **실패한 {len(pass1_failed_list)}개 계정에 대해 2차 재시도를 진행합니다...**"
                )
            except Exception:
                pass

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = await context.new_page()

                for f_user in pass1_failed_list:
                    if cancel_mass_redeem:
                        break

                    f_uid = f_user.get("uid")
                    f_server = f_user.get("server")

                    if not f_uid or not f_server:
                        final_failed_list.append(f_user)
                        continue

                    retry_success = await execute_redeem_with_page(page, str(f_uid), f_server, gift_code, max_retries=3)

                    if retry_success:
                        success_count += 1
                        fail_count -= 1
                        print(f"🎉 [2차 재시도 성공!] UID: {f_uid}")
                    else:
                        final_failed_list.append(f_user)

                await page.close()
                await browser.close()
        except Exception as retry_err:
            print(f"❌ 2차 재시도 루프 에러: {retry_err}")
            final_failed_list = pass1_failed_list
    else:
        final_failed_list = pass1_failed_list

    if sheet_failed and final_failed_list:
        for f_user in final_failed_list:
            if f_user.get("_raw_row"):
                try:
                    sheet_failed.append_row(f_user["_raw_row"])
                except Exception as s_err:
                    print(f"⚠️ Failed sheet append error: {s_err}")

    if cancel_mass_redeem:
        if target_channel:
            await target_channel.send(
                f"🛑 **작업이 중간에 강제 중단되었습니다.**\n"
                f"- 코드: `{gift_code}`\n"
                f"- 처리된 계정: **{processed_count} / {total_users}**\n"
                f"- 성공: **{success_count}** | 실패: **{fail_count}**"
            )
    else:
        summary = f"Success {success_count} / Failed {fail_count}"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            sheet_codes.append_row([gift_code, summary, now_str])
        except Exception as sheet_err:
            print(f"Google Sheet save error: {sheet_err}")

        embed = discord.Embed(title="🎁 Gift Code Auto Redemption Completed", color=0x00FF00)
        embed.add_field(name="Gift Code", value=f"`{gift_code}`", inline=False)
        embed.add_field(name="Summary", value=f"Total Accounts: **{total_users}**\nSuccess: **{success_count}** | Failed: **{fail_count}**", inline=False)

        if final_failed_list:
            failed_text_list = []
            for f_user in final_failed_list[:15]:
                f_uid = f_user.get("uid", "N/A")
                f_server = f_user.get("server", "N/A")
                f_name = f_user.get("username", "")
                failed_text_list.append(f"• UID: `{f_uid}` (State: `#{f_server}` / {f_name})")
            
            failed_str = "\n".join(failed_text_list)
            if len(final_failed_list) > 15:
                failed_str += f"\n...외 {len(final_failed_list) - 15}개 계정 (구글 시트 `failed_users` 탭 참조)"

            embed.add_field(name="⚠️ Final Failed Account List", value=failed_str, inline=False)

        if target_channel:
            await target_channel.send(embed=embed)

# ==========================================
# 📡 모니터링 구역
# ==========================================
@tasks.loop(seconds=60)
async def monitor_coupon_channel():
    channel = bot.get_channel(MONITOR_CHANNEL_ID)
    report_channel = bot.get_channel(REPORT_CHANNEL_ID)

    if not channel or not sheet_codes:
        return

    try:
        async for message in channel.history(limit=5):
            found_codes = re.findall(r"Code:\s*([a-zA-Z0-9]{6,20})", message.content, re.IGNORECASE)
            if not found_codes:
                found_codes = re.findall(r'\b[a-zA-Z0-9]{6,20}\b', message.content)

            for code in found_codes:
                if code.lower() in ["http", "https", "redemption", "until", "valid", "page"]:
                    continue

                used_records = sheet_codes.get_all_records()
                if not any(row.get('code') == code for row in used_records):
                    await process_mass_redeem(code, report_channel)
    except Exception as e:
        print(f"Error during channel monitoring: {e}")

@monitor_coupon_channel.before_loop
async def before_monitor():
    await bot.wait_until_ready()

# ==========================================
# 💬 영문 슬래시 커맨드 구역 (wr_ 접두사 적용)
# ==========================================

# 1. Register Account / Alt
@bot.tree.command(name="wr_register", description="Register your WOS UID and State (Kingdom) number. (Allows multiple alts)")
@app_commands.describe(uid="Player ID (Digits)", server="State / Kingdom Number (Digits)")
async def register(interaction: discord.Interaction, uid: str, server: int):
    if not sheet_users:
        await interaction.response.send_message("❌ Google Sheet DB is not connected.", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    username = interaction.user.display_name
    uid_str = str(uid).strip()

    guild_name = "Direct Message"
    if interaction.guild:
        guild_name = interaction.guild.name
    elif interaction.guild_id:
        guild_obj = bot.get_guild(interaction.guild_id)
        if guild_obj:
            guild_name = guild_obj.name
        else:
            guild_name = f"Guild_{interaction.guild_id}"

    raw_users = sheet_users.get_all_values()

    for row_idx, row in enumerate(raw_users[1:], start=2):
        if len(row) >= 3 and row[2].strip() == uid_str:
            sheet_users.update_cell(row_idx, 1, discord_id)
            sheet_users.update_cell(row_idx, 2, username)
            sheet_users.update_cell(row_idx, 4, str(server))
            sheet_users.update_cell(row_idx, 5, guild_name)
            
            await interaction.response.send_message(
                f"🔄 **{username}**'s UID `{uid_str}` has been updated to State `{server}` (From: **{guild_name}**).",
                ephemeral=True
            )
            return

    sheet_users.append_row([discord_id, username, uid_str, str(server), guild_name])

    embed = discord.Embed(
        title="✅ Account Registration Complete",
        description=f"{interaction.user.mention} (**{username}**)'s account has been successfully registered.",
        color=0x3498DB
    )
    embed.add_field(name="UID", value=uid_str, inline=True)
    embed.add_field(name="State (Kingdom)", value=f"#{server}", inline=True)
    embed.add_field(name="Server (From)", value=guild_name, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# 2. Check My Accounts
@bot.tree.command(name="wr_myinfo", description="View all registered UIDs and State numbers for your account.")
async def my_info(interaction: discord.Interaction):
    if not sheet_users:
        await interaction.response.send_message("❌ Google Sheet DB is not connected.", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    raw_users = sheet_users.get_all_values()

    my_accounts = []
    if len(raw_users) > 1:
        for row in raw_users[1:]:
            if len(row) > 0 and str(row[0]).strip() == discord_id:
                uid = row[2] if len(row) > 2 else 'N/A'
                server = row[3] if len(row) > 3 else 'N/A'
                my_accounts.append((uid, server))

    if my_accounts:
        embed = discord.Embed(
            title=f"ℹ️ {interaction.user.display_name}'s Registered Accounts (Total: {len(my_accounts)})",
            color=0x3498DB
        )
        for idx, (uid, server) in enumerate(my_accounts, 1):
            embed.add_field(name=f"Account #{idx}", value=f"UID: `{uid}` / State: `#{server}`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message("❌ No registered account found. Use `/wr_register [UID] [State]` to register.", ephemeral=True)

# 3. Delete Specific UID (일반 유저용)
@bot.tree.command(name="wr_deleteinfo", description="Delete a specific registered UID from your account.")
@app_commands.describe(uid="Player UID to delete")
async def delete_my_uid(interaction: discord.Interaction, uid: str):
    if not sheet_users:
        await interaction.response.send_message("❌ Google Sheet DB is not connected.", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    uid_str = str(uid).strip()

    raw_users = sheet_users.get_all_values()
    row_to_delete = None

    if len(raw_users) > 1:
        for idx, row in enumerate(raw_users[1:], 2):
            if len(row) >= 3 and str(row[0]).strip() == discord_id and str(row[2]).strip() == uid_str:
                row_to_delete = idx
                break

    if row_to_delete:
        sheet_users.delete_rows(row_to_delete)
        await interaction.response.send_message(f"🗑️ Successfully deleted UID `{uid_str}` from your account.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ UID `{uid_str}` was not found in your registered accounts.", ephemeral=True)

# 4. View Gift Code History
@bot.tree.command(name="wr_history", description="Check recently processed gift code redemption history.")
async def show_history(interaction: discord.Interaction):
    if not sheet_codes:
        await interaction.response.send_message("❌ Google Sheet DB is not connected.", ephemeral=True)
        return

    records = sheet_codes.get_all_records()
    if not records:
        await interaction.response.send_message("📜 No gift code redemption history found yet.", ephemeral=True)
        return

    embed = discord.Embed(title="📜 Recent Gift Code Redemption History (Last 10)", color=0x9B59B6)
    for row in reversed(records[-10:]):
        embed.add_field(
            name=f"🎁 Code: `{row.get('code')}`",
            value=f"└ **Result:** {row.get('result_summary')}\n└ **Date:** {row.get('used_at')}",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# 5. Total Registered User Count
@bot.tree.command(name="wr_usercount", description="Check total registered accounts for auto redemption.")
async def user_count(interaction: discord.Interaction):
    if not sheet_users:
        await interaction.response.send_message("❌ Google Sheet DB is not connected.", ephemeral=True)
        return

    raw_users = sheet_users.get_all_values()
    count = max(0, len(raw_users) - 1)

    embed = discord.Embed(
        title="👥 Auto Redemption Status",
        description=f"Currently **{count} account(s)** are registered for automatic redemption.",
        color=0x1ABC9C
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# 6. [Admin] Manual Redeem Code
@bot.tree.command(name="wr_sendcoupon", description="[Admin] Manually trigger gift code redemption for all registered accounts.")
@app_commands.describe(gift_code="Gift code to redeem")
@app_commands.checks.has_permissions(administrator=True)
async def manual_redeem(interaction: discord.Interaction, gift_code: str):
    await interaction.response.send_message(f"⏳ Starting manual redemption for gift code (`{gift_code}`)...", ephemeral=True)
    asyncio.create_task(process_mass_redeem(gift_code, interaction.channel))

# 7. [Admin] Delete Specific User/UID (관리자용)
@bot.tree.command(name="wr_deleteuser", description="[Admin] Delete a specific registered UID from the database.")
@app_commands.describe(uid="Player UID to delete from DB")
@app_commands.checks.has_permissions(administrator=True)
async def delete_user(interaction: discord.Interaction, uid: str):
    if not sheet_users:
        await interaction.response.send_message("❌ Google Sheet DB is not connected.", ephemeral=True)
        return

    target_uid = str(uid).strip()
    raw_users = sheet_users.get_all_values()

    row_to_delete = None
    if len(raw_users) > 1:
        for idx, row in enumerate(raw_users[1:], 2):
            if len(row) >= 3 and str(row[2]).strip() == target_uid:
                row_to_delete = idx
                break

    if not row_to_delete:
        await interaction.response.send_message(f"❌ UID `{target_uid}` was not found in the database.", ephemeral=True)
        return

    sheet_users.delete_rows(row_to_delete)

    embed = discord.Embed(
        title="🗑️ User Data Deleted",
        description=f"Admin successfully deleted UID `{target_uid}` from the database.",
        color=0xE74C3C
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@delete_user.error
async def delete_user_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("🚫 This command requires Administrator permissions.", ephemeral=True)

# 8. [Admin] 타인 서버 강제 퇴장 커맨드
@bot.tree.command(name="wr_leave_others", description="[Admin] 내 서버를 제외한 다른 서버에서 봇을 퇴장시킵니다.")
@app_commands.checks.has_permissions(administrator=True)
async def leave_other_guilds(interaction: discord.Interaction):
    current_guild_id = interaction.guild_id  # 명령어를 친 내 서버 ID
    
    left_count = 0
    for guild in bot.guilds:
        if guild.id != current_guild_id:
            await guild.leave()
            left_count += 1

    await interaction.response.send_message(
        f"🧹 현재 서버를 제외한 **{left_count}개 타인 서버**에서 봇이 성공적으로 퇴장했습니다!", 
        ephemeral=True
    )

@leave_other_guilds.error
async def leave_other_guilds_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("🚫 This command requires Administrator permissions.", ephemeral=True)

# 9. 대량 교환 강제 중단 커맨드
@bot.tree.command(name="wr_stop", description="진행 중인 대량 쿠폰 교환 작업을 강제 중단합니다.")
async def stop_redeem(interaction: discord.Interaction):
    global cancel_mass_redeem
    cancel_mass_redeem = True
    await interaction.response.send_message("🛑 **쿠폰 교환 중단 요청이 접수되었습니다.** 현재 처리 중인 건까지만 완료 후 중단됩니다.", ephemeral=True)

# ==========================================
# 🚀 메인 실행
# ==========================================
if __name__ == "__main__":
    keep_alive()
    bot.run(BOT_TOKEN)