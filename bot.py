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

# ==========================================
# 📊 구글 시트 DB 세팅
# ==========================================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

sheet_users = None
sheet_codes = None

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
    sheet_codes = sh.worksheet("used_codes")
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
        print("✅ 슬래시 명령어 동기화 및 모니터링 루프가 시작되었습니다.")

bot = WOSBot()

# ==========================================
# 🔄 Playwright 자동 입력 로직
# ==========================================
async def execute_redeem_with_page(page, uid: str, server: int, gift_code: str, max_retries: int = 2) -> bool:
    for attempt in range(1, max_retries + 1):
        try:
            await page.goto("https://wos-giftcode.centurygame.com/", timeout=10000)
            await page.wait_for_timeout(1000)

            await page.fill("input[placeholder='플레이어 ID']", str(uid), timeout=5000)
            await page.fill("input[placeholder='왕국']", str(server), timeout=5000)
            await page.fill("input[placeholder='교환 코드를 입력해 주세요']", str(gift_code), timeout=5000)

            confirm_btn = page.locator("div.exchange_btn")
            await confirm_btn.click(timeout=5000)
            await page.wait_for_timeout(2000)

            content = await page.content()

            if "성공" in content or "SUCCESS" in content.upper() or "발송" in content:
                return True
            else:
                print(f"⚠️ [시도 {attempt}/{max_retries}] 교환 응답 미확인 (UID: {uid}, 서버: {server})")

        except Exception as e:
            print(f"❌ [시도 {attempt}/{max_retries}] 입력 중 에러 발생 (UID: {uid}): {e}")

        if attempt < max_retries:
            await asyncio.sleep(1)

    return False

async def process_mass_redeem(gift_code: str, target_channel):
    if not sheet_users or not sheet_codes:
        if target_channel:
            await target_channel.send("❌ 구글 시트 연동 상태가 올바르지 않습니다.")
        return

    gift_code = gift_code.strip()

    # 1. 시트 데이터 로드 및 헤더 공백 자동 제거
    try:
        raw_users = sheet_users.get_all_values()
        if len(raw_users) <= 1:
            if target_channel:
                await target_channel.send("❌ DB(구글시트)에 등록된 유저가 없어 교환을 진행하지 않습니다.")
            return

        # 헤더명 양쪽 공백 제거
        headers = [str(h).strip().lower() for h in raw_users[0]]
        users_records = []
        
        for row in raw_users[1:]:
            if not row or not any(row):
                continue
            user_dict = {}
            for i, h in enumerate(headers):
                if i < len(row):
                    user_dict[h] = str(row[i]).strip()
            users_records.append(user_dict)

        used_codes_records = sheet_codes.get_all_records()

    except Exception as e:
        print(f"❌ 구글 시트 데이터 로드 실패: {e}")
        if target_channel:
            await target_channel.send(f"❌ 구글 시트 데이터를 읽는 중 에러가 발생했습니다: {e}")
        return

    # 2. 중복 코드 확인
    if any(str(row.get('code')).strip() == gift_code for row in used_codes_records):
        if target_channel:
            await target_channel.send(f"⚠️ `{gift_code}` 코드는 이미 처리된 히스토리가 있습니다.")
        return

    total_users = len(users_records)
    status_msg = None
    if target_channel:
        status_msg = await target_channel.send(f"🚀 **새로운 기프트코드 발견!** [`{gift_code}`]\n총 **{total_users}명** 자동 입력 작업을 시작합니다.")

    success_count = 0
    fail_count = 0

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

            for idx, user in enumerate(users_records, 1):
                uid = user.get("uid")
                server = user.get("server")

                if not uid or not server:
                    print(f"⚠️ [{idx}/{total_users}] UID/서버 누락 스킵: {user}")
                    fail_count += 1
                    continue

                page = await context.new_page()
                try:
                    is_success = await execute_redeem_with_page(page, str(uid), server, gift_code)
                except Exception as e:
                    print(f"❌ [{idx}/{total_users}] 예외 발생 (UID: {uid}): {e}")
                    is_success = False
                finally:
                    await page.close()

                if is_success:
                    success_count += 1
                else:
                    fail_count += 1

                if status_msg:
                    try:
                        await status_msg.edit(content=f"🔄 **자동 입력 진행 중...** [`{gift_code}`] [{idx}/{total_users}]\n✅ 성공: {success_count}명 | ❌ 실패: {fail_count}명")
                    except Exception as edit_err:
                        print(f"메시지 수정 에러: {edit_err}")

                await asyncio.sleep(0.5)

            await browser.close()
    except Exception as pw_err:
        print(f"❌ Playwright 치명적 에러: {pw_err}")

    # 3. 결과 기록
    summary = f"성공 {success_count}명 / 실패 {fail_count}명"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        sheet_codes.append_row([gift_code, summary, now_str])
    except Exception as sheet_err:
        print(f"구글 시트 저장 에러: {sheet_err}")

    # 4. 결과 출력
    embed = discord.Embed(title="🎁 기프트코드 자동 교환 완료", color=0x00FF00)
    embed.add_field(name="기프트코드", value=f"`{gift_code}`", inline=False)
    embed.add_field(name="결과 요약", value=f"총 대상: **{total_users}**명\n성공: **{success_count}**명 / 실패: **{fail_count}**명", inline=False)

    if target_channel:
        await target_channel.send(embed=embed)

# ==========================================
# 📡 모니터링 및 커맨드 구역
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
        print(f"모니터링 중 에러 발생: {e}")

@monitor_coupon_channel.before_loop
async def before_monitor():
    await bot.wait_until_ready()

# 1. 유저 등록
@bot.tree.command(name="등록", description="본인의 WOS UID와 서버 번호(왕국)를 등록합니다.")
@app_commands.describe(uid="플레이어 ID (숫자)", server="왕국 번호 (숫자)")
async def register(interaction: discord.Interaction, uid: str, server: int):
    if not sheet_users:
        await interaction.response.send_message("❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    username = interaction.user.display_name

    cell = sheet_users.find(discord_id, in_column=1)
    if cell:
        sheet_users.update_cell(cell.row, 2, username)
        sheet_users.update_cell(cell.row, 3, str(uid))
        sheet_users.update_cell(cell.row, 4, server)
    else:
        sheet_users.append_row([discord_id, username, str(uid), server])

    embed = discord.Embed(
        title="✅ 등록 완료",
        description=f"{interaction.user.mention}(**{username}**)님의 정보가 구글 시트 DB에 저장되었습니다.",
        color=0x3498DB
    )
    embed.add_field(name="UID", value=uid, inline=True)
    embed.add_field(name="왕국(서버)", value=f"{server}번", inline=True)

    await interaction.response.send_message(embed=embed)

# 2. 내 정보
@bot.tree.command(name="내정보", description="현재 등록되어 있는 내 정보를 확인합니다.")
async def my_info(interaction: discord.Interaction):
    if not sheet_users:
        await interaction.response.send_message("❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    raw_users = sheet_users.get_all_values()
    
    user_info = None
    if len(raw_users) > 1:
        for row in raw_users[1:]:
            if len(row) > 0 and str(row[0]).strip() == discord_id:
                user_info = row
                break

    if user_info:
        username = user_info[1] if len(user_info) > 1 else '미기록'
        uid = user_info[2] if len(user_info) > 2 else '미기록'
        server = user_info[3] if len(user_info) > 3 else '미기록'
        await interaction.response.send_message(f"ℹ️ **등록 정보**: `{username}` | UID `{uid}` / 왕국 `{server}`번")
    else:
        await interaction.response.send_message("❌ 등록된 정보가 없습니다. `/등록 [UID] [왕국번호]` 명령어로 등록해주세요.")

# 3. 히스토리
@bot.tree.command(name="히스토리", description="최근 봇이 처리한 기프트코드 교환 내역을 확인합니다.")
async def show_history(interaction: discord.Interaction):
    if not sheet_codes:
        await interaction.response.send_message("❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True)
        return

    records = sheet_codes.get_all_records()
    if not records:
        await interaction.response.send_message("📜 아직 교환 처리된 기프트코드 내역이 없습니다.")
        return

    embed = discord.Embed(title="📜 최근 기프트코드 교환 히스토리 (최근 10개)", color=0x9B59B6)
    for row in reversed(records[-10:]):
        embed.add_field(
            name=f"🎁 코드: `{row.get('code')}`",
            value=f"└ **결과:** {row.get('result_summary')}\n└ **일시:** {row.get('used_at')}",
            inline=False
        )
    await interaction.response.send_message(embed=embed)

# 4. 유저목록 요약
@bot.tree.command(name="유저목록", description="현재 자동 교환에 등록된 총 유저 수 현황을 확인합니다.")
async def user_count(interaction: discord.Interaction):
    if not sheet_users:
        await interaction.response.send_message("❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True)
        return

    raw_users = sheet_users.get_all_values()
    count = max(0, len(raw_users) - 1)

    embed = discord.Embed(
        title="👥 자동 교환 등록 현황",
        description=f"현재 총 **{count}명**의 플레이어가 기프트코드 자동 교환에 등록되어 있습니다.",
        color=0x1ABC9C
    )
    await interaction.response.send_message(embed=embed)

# 5. 수동 쿠폰 발송
@bot.tree.command(name="쿠폰발송", description="[관리자] 기프트코드를 입력하여 전체 유저에게 일괄 등록합니다.")
@app_commands.describe(gift_code="교환할 기프트코드")
@app_commands.checks.has_permissions(administrator=True)
async def manual_redeem(interaction: discord.Interaction, gift_code: str):
    await interaction.response.send_message(f"⏳ 기프트코드(`{gift_code}`) 수동 발송 작업을 시작합니다...")
    asyncio.create_task(process_mass_redeem(gift_code, interaction.channel))

# 6. 유저삭제
@bot.tree.command(name="유저삭제", description="[관리자] 특정 유저의 등록 정보를 삭제합니다.")
@app_commands.describe(target_user="삭제할 디스코드 유저")
@app_commands.checks.has_permissions(administrator=True)
async def delete_user(interaction: discord.Interaction, target_user: discord.User):
    if not sheet_users:
        await interaction.response.send_message("❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True)
        return

    discord_id = str(target_user.id)
    cell = sheet_users.find(discord_id, in_column=1)
    if not cell:
        await interaction.response.send_message(f"❌ {target_user.mention}님은 등록된 정보가 없습니다.", ephemeral=True)
        return

    sheet_users.delete_rows(cell.row)
    embed = discord.Embed(
        title="🗑️ 유저 정보 삭제 완료",
        description=f"{target_user.mention}님의 등록 정보를 구글 시트에서 삭제했습니다.",
        color=0xE74C3C
    )
    await interaction.response.send_message(embed=embed)

# ==========================================
# 🚀 메인 실행
# ==========================================
if __name__ == "__main__":
    keep_alive()
    bot.run(BOT_TOKEN)