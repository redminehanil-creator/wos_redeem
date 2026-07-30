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
# ⚡ 브라우저 자동 검사/설치 (1초 자동체크)
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
# 🔄 Playwright 자동 입력 로직 (최신 사이트 폼 반영)
# ==========================================
async def execute_redeem_with_page(
    page, uid: str, server: int, gift_code: str, max_retries: int = 2
) -> bool:
    """초고속 스마트 대기 적용 WOS 교환 자동화"""
    for attempt in range(1, max_retries + 1):
        try:
            # 1. 사이트 접속
            await page.goto(
                "https://wos-giftcode.centurygame.com/",
                timeout=15000,
                wait_until="domcontentloaded",
            )

            # 2. 값 입력 (각 500ms 타임아웃 유지)
            uid_input = page.locator("input[placeholder='플레이어 ID']").first
            await uid_input.fill(str(uid), timeout=1000)

            server_input = page.locator("input[placeholder='왕국']").first
            await server_input.fill(str(server), timeout=1000)

            code_input = page.locator(
                "input[placeholder='교환 코드를 입력해 주세요']"
            ).first
            await code_input.fill(str(gift_code), timeout=1000)

            # 3. 교환 버튼 클릭
            exchange_btn = page.locator("div.exchange_btn").first
            await exchange_btn.click(timeout=3000)

            # 4. ⚡ 팝업이 나타나는 순간 '즉시' 읽어오기 (스마트 대기)
            # 고정 지연시간 대신 팝업창 요소가 화면에 뜨는 찰나를 감지합니다.
            try:
                msg_locator = page.locator(
                    "p.msg, div.modal_content, div.msg"
                ).first
                await msg_locator.wait_for(state="visible", timeout=4000)
                popup_text = await msg_locator.text_content()
            except Exception:
                popup_text = await page.content()

            popup_text = popup_text.strip()
            print(f"🔍 [UID: {uid}] 팝업 응답: {popup_text}")

            # -----------------------------------------------------------
            # 🎯 팝업 메시지 정밀 판정 (성공 / 이미 수령 / 오류 / 만료)
            # -----------------------------------------------------------
            if any(
                k in popup_text
                for k in [
                    "교환 성공",
                    "우편에서 보상",
                    "보상을 확인하세요",
                    "SUCCESS",
                    "Claimed",
                ]
            ):
                print(f"✅ [UID: {uid} / 서버: {server}] 교환 성공!")
                return True

            elif any(
                k in popup_text
                for k in ["이미 수령", "다시 수령", "ALREADY", "RECEIVED"]
            ):
                print(f"ℹ️ [UID: {uid}] 이미 수령한 쿠폰입니다. (성공 처리)")
                return True

            elif any(
                k in popup_text
                for k in ["존재하지 않습니다", "대소문자", "시간이 초과", "만료"]
            ):
                print(f"❌ [UID: {uid}] 유효하지 않거나 만료된 코드입니다.")
                return False

            else:
                print(
                    f"⚠️ [시도 {attempt}/{max_retries}] 감지되지 않은 문구: {popup_text}"
                )

        except Exception as e:
            print(
                f"❌ [시도 {attempt}/{max_retries}] 진행 중 에러 발생 (UID: {uid}): {e}"
            )

        if attempt < max_retries:
            await asyncio.sleep(0.5)

    return False
    
async def process_mass_redeem(gift_code: str, target_channel):
    if not sheet_users or not sheet_codes:
        if target_channel:
            await target_channel.send("❌ 구글 시트 연동 상태가 올바르지 않습니다.")
        return

    gift_code = gift_code.strip()

    # 1. 시트 데이터 로드
    try:
        raw_users = sheet_users.get_all_values()
        if len(raw_users) <= 1:
            if target_channel:
                await target_channel.send("❌ DB(구글시트)에 등록된 유저가 없어 교환을 진행하지 않습니다.")
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
        status_msg = await target_channel.send(f"🚀 **새로운 기프트코드 발견!** [`{gift_code}`]\n총 **{total_users}개 계정** 자동 입력 작업을 시작합니다.")

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
                        await status_msg.edit(content=f"🔄 **자동 입력 진행 중...** [`{gift_code}`] [{idx}/{total_users}]\n✅ 성공: {success_count}개 | ❌ 실패: {fail_count}개")
                    except Exception as edit_err:
                        print(f"메시지 수정 에러: {edit_err}")

                await asyncio.sleep(0.5)

            await browser.close()
    except Exception as pw_err:
        print(f"❌ Playwright 치명적 에러: {pw_err}")

    # 3. 결과 기록
    summary = f"성공 {success_count}개 / 실패 {fail_count}개"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        sheet_codes.append_row([gift_code, summary, now_str])
    except Exception as sheet_err:
        print(f"구글 시트 저장 에러: {sheet_err}")

    # 4. 결과 출력
    embed = discord.Embed(title="🎁 기프트코드 자동 교환 완료", color=0x00FF00)
    embed.add_field(name="기프트코드", value=f"`{gift_code}`", inline=False)
    embed.add_field(name="결과 요약", value=f"총 대상 계정: **{total_users}**개\n성공: **{success_count}**개 / 실패: **{fail_count}**개", inline=False)

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
        print(f"모니터링 중 에러 발생: {e}")

@monitor_coupon_channel.before_loop
async def before_monitor():
    await bot.wait_until_ready()

# ==========================================
# 💬 슬래시 커맨드 구역 (다중 UID 지원)
# ==========================================

# 1. 유저/부계정 등록 (중복 가능 및 추가 등록)
@bot.tree.command(name="등록", description="WOS UID와 서버 번호를 등록합니다. (부계정 추가 등록 가능)")
@app_commands.describe(uid="플레이어 ID (숫자)", server="왕국 번호 (숫자)")
async def register(interaction: discord.Interaction, uid: str, server: int):
    if not sheet_users:
        await interaction.response.send_message("❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    username = interaction.user.display_name
    uid_str = str(uid).strip()

    raw_users = sheet_users.get_all_values()
    
    # 이미 동일한 UID가 등록되어 있는지 확인
    for row in raw_users[1:]:
        if len(row) >= 3 and row[2].strip() == uid_str:
            # 이미 존재하는 UID라면 해당 행 업데이트
            cell = sheet_users.find(uid_str, in_column=3)
            if cell:
                sheet_users.update_cell(cell.row, 1, discord_id)
                sheet_users.update_cell(cell.row, 2, username)
                sheet_users.update_cell(cell.row, 4, server)
                await interaction.response.send_message(f"🔄 **{username}**님의 UID `{uid_str}` 정보가 최신(왕국: {server}번)으로 수정되었습니다.")
                return

    # 신규 UID 추가 (한 유저가 여러 행 보유 가능)
    sheet_users.append_row([discord_id, username, uid_str, server])

    embed = discord.Embed(
        title="✅ 계정 등록 완료",
        description=f"{interaction.user.mention}(**{username}**)님의 계정이 추가 등록되었습니다.",
        color=0x3498DB
    )
    embed.add_field(name="UID", value=uid_str, inline=True)
    embed.add_field(name="왕국(서버)", value=f"{server}번", inline=True)

    await interaction.response.send_message(embed=embed)

# 2. 내 정보 확인 (등록된 모든 계정 출력)
@bot.tree.command(name="내정보", description="현재 내가 등록한 모든 UID 및 왕국 목록을 확인합니다.")
async def my_info(interaction: discord.Interaction):
    if not sheet_users:
        await interaction.response.send_message("❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    raw_users = sheet_users.get_all_values()
    
    my_accounts = []
    if len(raw_users) > 1:
        for row in raw_users[1:]:
            if len(row) > 0 and str(row[0]).strip() == discord_id:
                uid = row[2] if len(row) > 2 else '미기록'
                server = row[3] if len(row) > 3 else '미기록'
                my_accounts.append((uid, server))

    if my_accounts:
        embed = discord.Embed(
            title=f"ℹ️ {interaction.user.display_name}님의 등록 계정 목록 (총 {len(my_accounts)}개)",
            color=0x3498DB
        )
        for idx, (uid, server) in enumerate(my_accounts, 1):
            embed.add_field(name=f"계정 #{idx}", value=f"UID: `{uid}` / 왕국: `{server}`번", inline=False)
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("❌ 등록된 정보가 없습니다. `/등록 [UID] [왕국번호]` 명령어로 계정을 등록해주세요.")

# 3. 내 특정 UID 삭제
@bot.tree.command(name="내정보삭제", description="본인이 등록한 특정 UID 계정을 삭제합니다.")
@app_commands.describe(uid="삭제할 플레이어 UID")
async def delete_my_uid(interaction: discord.Interaction, uid: str):
    if not sheet_users:
        await interaction.response.send_message("❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    uid_str = str(uid).strip()

    raw_users = sheet_users.get_all_values()
    row_to_delete = None

    if len(raw_users) > 1:
        for idx, row in enumerate(raw_users[1:], 2): # 1행은 헤더이므로 index 2부터 시작
            if len(row) >= 3 and str(row[0]).strip() == discord_id and str(row[2]).strip() == uid_str:
                row_to_delete = idx
                break

    if row_to_delete:
        sheet_users.delete_rows(row_to_delete)
        await interaction.response.send_message(f"🗑️ 본인의 UID `{uid_str}` 계정 정보를 성공적으로 삭제했습니다.")
    else:
        await interaction.response.send_message(f"❌ 본인 계정 중 UID `{uid_str}` 정보를 찾을 수 없습니다.", ephemeral=True)

# 4. 히스토리
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

# 5. 총 등록 계정 수
@bot.tree.command(name="유저목록", description="현재 자동 교환에 등록된 총 계정 수 현황을 확인합니다.")
async def user_count(interaction: discord.Interaction):
    if not sheet_users:
        await interaction.response.send_message("❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True)
        return

    raw_users = sheet_users.get_all_values()
    count = max(0, len(raw_users) - 1)

    embed = discord.Embed(
        title="👥 자동 교환 등록 현황",
        description=f"현재 총 **{count}개**의 계정이 기프트코드 자동 교환에 등록되어 있습니다.",
        color=0x1ABC9C
    )
    await interaction.response.send_message(embed=embed)

# 6. [관리자] 수동 쿠폰 발송
@bot.tree.command(name="쿠폰발송", description="[관리자] 기프트코드를 입력하여 전체 유저에게 일괄 등록합니다.")
@app_commands.describe(gift_code="교환할 기프트코드")
@app_commands.checks.has_permissions(administrator=True)
async def manual_redeem(interaction: discord.Interaction, gift_code: str):
    await interaction.response.send_message(f"⏳ 기프트코드(`{gift_code}`) 수동 발송 작업을 시작합니다...")
    asyncio.create_task(process_mass_redeem(gift_code, interaction.channel))

# 7. [관리자] 특정 유저 전체 삭제
@bot.tree.command(name="유저삭제", description="[관리자] 특정 유저의 모든 등록 정보를 삭제합니다.")
@app_commands.describe(target_user="삭제할 디스코드 유저")
@app_commands.checks.has_permissions(administrator=True)
async def delete_user(interaction: discord.Interaction, target_user: discord.User):
    if not sheet_users:
        await interaction.response.send_message("❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True)
        return

    discord_id = str(target_user.id)
    raw_users = sheet_users.get_all_values()
    
    rows_to_delete = []
    if len(raw_users) > 1:
        for idx, row in enumerate(raw_users[1:], 2):
            if len(row) > 0 and str(row[0]).strip() == discord_id:
                rows_to_delete.append(idx)

    if not rows_to_delete:
        await interaction.response.send_message(f"❌ {target_user.mention}님은 등록된 정보가 없습니다.", ephemeral=True)
        return

    for r in reversed(rows_to_delete):
        sheet_users.delete_rows(r)

    embed = discord.Embed(
        title="🗑️ 유저 정보 삭제 완료",
        description=f"{target_user.mention}님의 등록 정보(총 {len(rows_to_delete)}개)를 삭제했습니다.",
        color=0xE74C3C
    )
    await interaction.response.send_message(embed=embed)

# ==========================================
# 🚀 메인 실행
# ==========================================
if __name__ == "__main__":
    keep_alive()
    bot.run(BOT_TOKEN)