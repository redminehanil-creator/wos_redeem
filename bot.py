import asyncio
import json
import os
import re
import sys
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
# ⚙️ 기본 설정 구역
# ==========================================
sys.stdout.reconfigure(line_buffering=True)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_DISCORD_BOT_TOKEN_HERE")
MONITOR_CHANNEL_ID = 973162050333327390  # 모니터링할 채널 ID
REPORT_CHANNEL_ID = 1532160917943484626  # 결과를 보고받을 채널 ID

# 구글 시트 이름
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "wos_bot_db")

# 환경변수에 저장된 GOOGLE_JSON 파싱
GOOGLE_JSON_RAW = os.environ.get("GOOGLE_JSON", "")

# ==========================================
# 📊 구글 시트(Google Sheets) DB 세팅
# ==========================================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

sheet_users = None
sheet_codes = None

try:
    if GOOGLE_JSON_RAW:
        creds_dict = json.loads(GOOGLE_JSON_RAW)

        # private_key 내부의 \n 문자열을 실제 줄바꿈 문자로 변환
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace(
                "\\n", "\n"
            )

        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            creds_dict, scope
        )
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            "service_account.json", scope
        )

    gc = gspread.authorize(creds)
    sh = gc.open(SPREADSHEET_NAME)

    sheet_users = sh.sheet1  # 첫번째 시트 (users)
    sheet_codes = sh.worksheet("used_codes")  # 두번째 시트 (used_codes)
    print("✅ 구글 시트 데이터베이스 연동 성공!")
except Exception as e:
    print(f"❌ [초기화 에러] 구글 시트 연동 실패: {e}")

# ==========================================
# 🌐 Render Web Service 유지용 Flask 웹 서버
# ==========================================
web_app = Flask("")


@web_app.route("/")
def home():
    return "WOS Discord Bot is Active and Running!"


def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)


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
# 🔄 Playwright 자동 입력 로직 (타임아웃 강화)
# ==========================================
async def execute_redeem_with_page(
    page, uid: str, server: int, gift_code: str, max_retries: int = 2
) -> bool:
    """단일 page 객체를 사용해 교환 시도"""
    for attempt in range(1, max_retries + 1):
        try:
            await page.goto(
                "https://wos-giftcode.centurygame.com/", timeout=10000
            )
            await page.wait_for_timeout(1000)

            await page.fill(
                "input[placeholder='플레이어 ID']", str(uid), timeout=5000
            )
            await page.fill(
                "input[placeholder='왕국']", str(server), timeout=5000
            )
            await page.fill(
                "input[placeholder='교환 코드를 입력해 주세요']",
                str(gift_code),
                timeout=5000,
            )

            confirm_btn = page.locator("div.exchange_btn")
            await confirm_btn.click(timeout=5000)
            await page.wait_for_timeout(2000)

            content = await page.content()

            if (
                "성공" in content
                or "SUCCESS" in content.upper()
                or "발송" in content
            ):
                return True
            else:
                print(
                    f"⚠️ [시도 {attempt}/{max_retries}] 교환 실패/응답 미확인 (UID: {uid}, 서버: {server})"
                )

        except Exception as e:
            print(
                f"❌ [시도 {attempt}/{max_retries}] 입력 중 에러 발생 (UID: {uid}): {e}"
            )

        if attempt < max_retries:
            await asyncio.sleep(1)

    return False


async def process_mass_redeem(gift_code: str, target_channel):
    """구글 시트의 모든 유저 목록을 가져와 일괄 등록"""
    if not sheet_users or not sheet_codes:
        if target_channel:
            await target_channel.send(
                "❌ 구글 시트 연동 상태가 올바르지 않습니다."
            )
        return

    gift_code = gift_code.strip()

    # 1. 이미 사용된 코드인지 구글 시트 확인
    used_records = sheet_users.get_all_records()
    used_codes_records = sheet_codes.get_all_records()

    if any(row.get("code") == gift_code for row in used_codes_records):
        if target_channel:
            await target_channel.send(
                f"⚠️ `{gift_code}` 코드는 이미 처리된 히스토리가 있습니다."
            )
        return

    if not used_records:
        if target_channel:
            await target_channel.send(
                "❌ DB(구글시트)에 등록된 유저가 없어 교환을 진행하지 않습니다."
            )
        return

    status_msg = None
    if target_channel:
        status_msg = await target_channel.send(
            f"🚀 **새로운 기프트코드 발견!** [`{gift_code}`]\n총 **{len(used_records)}명** 자동 입력 작업을 시작합니다."
        )

    success_count = 0
    fail_count = 0

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

            for idx, user in enumerate(used_records, 1):
                uid = user.get("uid")
                server = user.get("server")

                page = await context.new_page()
                try:
                    is_success = await execute_redeem_with_page(
                        page, str(uid), server, gift_code
                    )
                except Exception as e:
                    print(f"❌ [{idx}/{len(used_records)}] 작업 중 예외 발생: {e}")
                    is_success = False
                finally:
                    await page.close()

                if is_success:
                    success_count += 1
                else:
                    fail_count += 1

                # 💡 유저 1명 처리할 때마다 실시간으로 디스코드 메시지 수정
                if status_msg:
                    try:
                        await status_msg.edit(
                            content=f"🔄 **자동 입력 진행 중...** [`{gift_code}`] [{idx}/{len(used_records)}]\n✅ 성공: {success_count}명 | ❌ 실패: {fail_count}명"
                        )
                    except Exception as edit_err:
                        print(f"메시지 수정 에러: {edit_err}")

                await asyncio.sleep(0.5)

            await browser.close()
    except Exception as pw_err:
        print(f"❌ Playwright 실행 중 치명적 에러: {pw_err}")

    # 3. 구글 시트 히스토리에 기록
    summary = f"성공 {success_count}명 / 실패 {fail_count}명"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        sheet_codes.append_row([gift_code, summary, now_str])
    except Exception as sheet_err:
        print(f"구글 시트 저장 에러: {sheet_err}")

    # 4. 결과 최종 출력
    embed = discord.Embed(
        title="🎁 기프트코드 자동 교환 완료", color=0x00FF00
    )
    embed.add_field(name="기프트코드", value=f"`{gift_code}`", inline=False)
    embed.add_field(
        name="결과 요약",
        value=f"총 대상: **{len(used_records)}**명\n성공: **{success_count}**명 / 실패: **{fail_count}**명",
        inline=False,
    )

    if target_channel:
        await target_channel.send(embed=embed)
        
    # 3. 구글 시트 히스토리에 기록
    summary = f"성공 {success_count}명 / 실패 {fail_count}명"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheet_codes.append_row([gift_code, summary, now_str])

    # 4. 결과 출력
    embed = discord.Embed(
        title="🎁 기프트코드 자동 교환 완료", color=0x00FF00
    )
    embed.add_field(name="기프트코드", value=f"`{gift_code}`", inline=False)
    embed.add_field(
        name="결과 요약",
        value=f"총 대상: **{len(users_records)}**명\n성공: **{success_count}**명 / 실패: **{fail_count}**명",
        inline=False,
    )

    if target_channel:
        await target_channel.send(embed=embed)


# ==========================================
# 📡 디스코드 채널 모니터링
# ==========================================
@tasks.loop(seconds=60)
async def monitor_coupon_channel():
    channel = bot.get_channel(MONITOR_CHANNEL_ID)
    report_channel = bot.get_channel(REPORT_CHANNEL_ID)

    if not channel or not sheet_codes:
        return

    try:
        async for message in channel.history(limit=5):
            found_codes = re.findall(
                r"Code:\s*([a-zA-Z0-9]{6,20})", message.content, re.IGNORECASE
            )

            if not found_codes:
                found_codes = re.findall(
                    r"\b[a-zA-Z0-9]{6,20}\b", message.content
                )

            for code in found_codes:
                if code.lower() in [
                    "http",
                    "https",
                    "redemption",
                    "until",
                    "valid",
                    "page",
                ]:
                    continue

                used_records = sheet_codes.get_all_records()
                if not any(row.get("code") == code for row in used_records):
                    await process_mass_redeem(code, report_channel)
    except Exception as e:
        print(f"모니터링 중 에러 발생: {e}")


@monitor_coupon_channel.before_loop
async def before_monitor():
    await bot.wait_until_ready()


# ==========================================
# 💬 슬래시 커맨드 (프로필 이름 추가 기록)
# ==========================================


# 1. 일반 유저 등록
@bot.tree.command(
    name="등록", description="본인의 WOS UID와 서버 번호(왕국)를 등록합니다."
)
@app_commands.describe(uid="플레이어 ID (숫자)", server="왕국 번호 (숫자)")
async def register(interaction: discord.Interaction, uid: str, server: int):
    if not sheet_users:
        await interaction.response.send_message(
            "❌ DB(구글시트)가 연결되지 않았습니다. 관리자에게 문의하세요.",
            ephemeral=True,
        )
        return

    discord_id = str(interaction.user.id)
    username = interaction.user.display_name  # 디스코드 프로필 이름(닉네임)

    # 구글 시트에 기존 등록 유저 확인
    cell = sheet_users.find(discord_id, in_column=1)
    if cell:
        # 기존 유저 정보 업데이트 (A: discord_id, B: username, C: uid, D: server)
        sheet_users.update_cell(cell.row, 2, username)
        sheet_users.update_cell(cell.row, 3, str(uid))
        sheet_users.update_cell(cell.row, 4, server)
    else:
        # 신규 유저 추가 [discord_id, username, uid, server]
        sheet_users.append_row([discord_id, username, str(uid), server])

    embed = discord.Embed(
        title="✅ 등록 완료",
        description=f"{interaction.user.mention}(**{username}**)님의 정보가 구글 시트 DB에 저장되었습니다.",
        color=0x3498DB,
    )
    embed.add_field(name="UID", value=uid, inline=True)
    embed.add_field(name="왕국(서버)", value=f"{server}번", inline=True)

    await interaction.response.send_message(embed=embed)


# 2. 내 정보 확인
@bot.tree.command(
    name="내정보", description="현재 등록되어 있는 내 정보를 확인합니다."
)
async def my_info(interaction: discord.Interaction):
    if not sheet_users:
        await interaction.response.send_message(
            "❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True
        )
        return

    discord_id = str(interaction.user.id)
    records = sheet_users.get_all_records()
    user_info = next(
        (row for row in records if str(row.get("discord_id")) == discord_id),
        None,
    )

    if user_info:
        await interaction.response.send_message(
            f"ℹ️ **등록 정보**: `{user_info.get('username', '미기록')}` | UID `{user_info.get('uid')}` / 왕국 `{user_info.get('server')}`번"
        )
    else:
        await interaction.response.send_message(
            "❌ 등록된 정보가 없습니다. `/등록 [UID] [왕국번호]` 명령어로 등록해주세요."
        )


# 3. 히스토리 확인
@bot.tree.command(
    name="히스토리",
    description="최근 봇이 처리한 기프트코드 교환 내역을 확인합니다.",
)
async def show_history(interaction: discord.Interaction):
    if not sheet_codes:
        await interaction.response.send_message(
            "❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True
        )
        return

    records = sheet_codes.get_all_records()

    if not records:
        await interaction.response.send_message(
            "📜 아직 교환 처리된 기프트코드 내역이 없습니다."
        )
        return

    embed = discord.Embed(
        title="📜 최근 기프트코드 교환 히스토리 (최근 10개)",
        color=0x9B59B6,
    )

    for row in reversed(records[-10:]):
        embed.add_field(
            name=f"🎁 코드: `{row.get('code')}`",
            value=f"└ **결과:** {row.get('result_summary')}\n└ **일시:** {row.get('used_at')}",
            inline=False,
        )

    await interaction.response.send_message(embed=embed)


# 4. 등록 인원 요약
@bot.tree.command(
    name="유저목록",
    description="현재 자동 교환에 등록된 총 유저 수 현황을 확인합니다.",
)
async def user_count(interaction: discord.Interaction):
    if not sheet_users:
        await interaction.response.send_message(
            "❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True
        )
        return

    records = sheet_users.get_all_records()

    embed = discord.Embed(
        title="👥 자동 교환 등록 현황",
        description=f"현재 총 **{len(records)}명**의 플레이어가 기프트코드 자동 교환에 등록되어 있습니다.",
        color=0x1ABC9C,
    )
    await interaction.response.send_message(embed=embed)


# 5. [관리자 전용] 등록된 특정 유저 삭제
@bot.tree.command(
    name="유저삭제", description="[관리자] 특정 유저의 등록 정보를 삭제합니다."
)
@app_commands.describe(target_user="삭제할 디스코드 유저")
@app_commands.checks.has_permissions(administrator=True)
async def delete_user(
    interaction: discord.Interaction, target_user: discord.User
):
    if not sheet_users:
        await interaction.response.send_message(
            "❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True
        )
        return

    discord_id = str(target_user.id)

    cell = sheet_users.find(discord_id, in_column=1)
    if not cell:
        await interaction.response.send_message(
            f"❌ {target_user.mention}님은 등록된 정보가 없습니다.",
            ephemeral=True,
        )
        return

    sheet_users.delete_rows(cell.row)

    embed = discord.Embed(
        title="🗑️ 유저 정보 삭제 완료",
        description=f"{target_user.mention}님의 등록 정보를 구글 시트에서 삭제했습니다.",
        color=0xE74C3C,
    )
    await interaction.response.send_message(embed=embed)


# 6. [관리자 전용] 전체 유저 상세 명단 조회
@bot.tree.command(
    name="전체유저목록",
    description="[관리자] DB에 등록된 전체 유저 명단을 조회합니다.",
)
@app_commands.checks.has_permissions(administrator=True)
async def full_user_list(interaction: discord.Interaction):
    if not sheet_users:
        await interaction.response.send_message(
            "❌ DB(구글시트)가 연결되지 않았습니다.", ephemeral=True
        )
        return

    records = sheet_users.get_all_records()

    if not records:
        await interaction.response.send_message(
            "📋 현재 DB에 등록된 유저가 없습니다.", ephemeral=True
        )
        return

    description_text = ""
    is_first = True

    for idx, user in enumerate(records, 1):
        discord_id = user.get("discord_id")
        username = user.get("username", "이름없음")
        uid = user.get("uid")
        server = user.get("server")

        description_text += f"**{idx}.** {username} (<@{discord_id}>) | UID: `{uid}` | 왕국: `{server}`번\n"

        if idx % 15 == 0 or idx == len(records):
            embed = discord.Embed(
                title=f"📋 등록 유저 명단 (총 {len(records)}명)",
                description=description_text,
                color=0x34495E,
            )
            if is_first:
                await interaction.response.send_message(
                    embed=embed, ephemeral=True
                )
                is_first = False
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)

            description_text = ""


# 7. [관리자 전용] 수동 쿠폰 발송
@bot.tree.command(
    name="쿠폰발송",
    description="[관리자] 기프트코드를 입력하여 전체 유저에게 일괄 등록합니다.",
)
@app_commands.describe(gift_code="교환할 기프트코드")
@app_commands.checks.has_permissions(administrator=True)
async def manual_redeem(interaction: discord.Interaction, gift_code: str):
    await interaction.response.send_message(
        f"⏳ 기프트코드(`{gift_code}`) 수동 발송 작업을 시작합니다..."
    )
    asyncio.create_task(
        process_mass_redeem(gift_code, interaction.channel)
    )


# ==========================================
# ⚠️ 에러 핸들러
# ==========================================
@register.error
@my_info.error
@show_history.error
@user_count.error
@delete_user.error
@full_user_list.error
@manual_redeem.error
async def global_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    print(
        f"❌ [{interaction.command.name}] 실행 중 상세 오류 발생: {error}"
    )

    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "🚫 이 명령어는 **서버 관리자** 권한이 있는 분만 사용하실 수 있습니다.",
            ephemeral=True,
        )
    else:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ 명령어 처리 중 에러가 발생했습니다. Render 콘솔 로그를 확인해 주세요.",
                ephemeral=True,
            )


# ==========================================
# 🚀 메인 실행 부분
# ==========================================
if __name__ == "__main__":
    keep_alive()
    bot.run(BOT_TOKEN)