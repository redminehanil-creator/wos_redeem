import asyncio
import os
import re
import sqlite3
import sys
from threading import Thread
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask
from playwright.async_api import async_playwright

# ==========================================
# ⚙️ 실시간 로그 및 기본 설정 구역
# ==========================================
# Render 콘솔에서 print() 로그가 즉시 출력되도록 버퍼링 해제
sys.stdout.reconfigure(line_buffering=True)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_DISCORD_BOT_TOKEN_HERE")

MONITOR_CHANNEL_ID = 973162050333327390  # 모니터링할 채널 ID
REPORT_CHANNEL_ID = 1532160917943484626  # 결과를 보고받을 채널 ID

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
# 🗄️ 데이터베이스 세팅 (SQLite)
# ==========================================
conn = sqlite3.connect("wos_users.db")
cursor = conn.cursor()

# 유저 정보 테이블
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    discord_id TEXT PRIMARY KEY,
    uid TEXT NOT NULL,
    server INTEGER NOT NULL
)
""")

# 사용된 기프트코드 기록 테이블
cursor.execute("""
CREATE TABLE IF NOT EXISTS used_codes (
    code TEXT PRIMARY KEY,
    result_summary TEXT DEFAULT '처리 완료',
    used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()


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
# 🔄 실제 HTML 기반 Playwright 자동 입력 (재시도 포함)
# ==========================================
async def execute_redeem_playwright(
    uid: str, server: int, gift_code: str, max_retries: int = 3
) -> bool:
    """웹사이트 HTML 요소에 직접 정확히 접근하는 교환 로직"""
    for attempt in range(1, max_retries + 1):
        async with async_playwright() as p:
            browser = None
            try:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = await context.new_page()

                # 1. 교환 센터 접속
                await page.goto(
                    "https://wos-giftcode.centurygame.com/", timeout=30000
                )
                await page.wait_for_timeout(1500)

                # 2. [플레이어 ID] 입력
                await page.fill("input[placeholder='플레이어 ID']", str(uid))

                # 3. [왕국] 입력
                await page.fill("input[placeholder='왕국']", str(server))

                # 4. [교환 코드] 입력
                await page.fill(
                    "input[placeholder='교환 코드를 입력해 주세요']",
                    str(gift_code),
                )

                # 5. [교환 확인] 버튼 클릭 (div.exchange_btn 클릭)
                confirm_btn = page.locator("div.exchange_btn")
                await confirm_btn.click()
                await page.wait_for_timeout(2500)

                # 6. 결과 확인
                content = await page.content()

                if (
                    "성공" in content
                    or "SUCCESS" in content.upper()
                    or "발송" in content
                ):
                    return True
                else:
                    print(
                        f"⚠️ [시도 {attempt}/{max_retries}] 교환 실패 (UID: {uid}, 서버: {server})"
                    )

            except Exception as e:
                print(
                    f"⚠️ [시도 {attempt}/{max_retries}] 입력 중 에러 (UID: {uid}): {e}"
                )
            finally:
                if browser:
                    await browser.close()

        if attempt < max_retries:
            await asyncio.sleep(1.5)

    return False


async def process_mass_redeem(gift_code: str, target_channel):
    """DB에 저장된 모든 유저에게 일괄 적용 및 진행 상황 리포트"""
    gift_code = gift_code.upper().strip()

    # 1. 이미 사용된 코드인지 DB 확인
    cursor.execute(
        "SELECT code FROM used_codes WHERE code = ?", (gift_code,)
    )
    if cursor.fetchone():
        if target_channel:
            await target_channel.send(
                f"⚠️ `{gift_code}` 코드는 이미 처리된 히스토리가 있습니다."
            )
        return

    # 2. 유저 목록 조회
    cursor.execute("SELECT uid, server FROM users")
    rows = cursor.fetchall()

    if not rows:
        if target_channel:
            await target_channel.send(
                "❌ DB에 등록된 유저가 없어 교환을 진행하지 않습니다."
            )
        return

    status_msg = None
    if target_channel:
        status_msg = await target_channel.send(
            f"🚀 **새로운 기프트코드 발견!** [`{gift_code}`]\n총 **{len(rows)}명** 자동 입력 작업을 시작합니다."
        )

    success_count = 0
    fail_count = 0

    for idx, (uid, server) in enumerate(rows, 1):
        is_success = await execute_redeem_playwright(uid, server, gift_code)

        if is_success:
            success_count += 1
        else:
            fail_count += 1

        if status_msg and (idx % 3 == 0 or idx == len(rows)):
            await status_msg.edit(
                content=f"🔄 **자동 입력 진행 중...** [`{gift_code}`] [{idx}/{len(rows)}]\n✅ 성공: {success_count}명 | ❌ 실패: {fail_count}명"
            )

        await asyncio.sleep(1)

    # 3. DB 기록
    summary = f"성공 {success_count}명 / 실패 {fail_count}명"
    cursor.execute(
        """
        INSERT OR REPLACE INTO used_codes (code, result_summary) VALUES (?, ?)
    """,
        (gift_code, summary),
    )
    conn.commit()

    # 4. 결과 출력
    embed = discord.Embed(
        title="🎁 기프트코드 자동 교환 완료", color=0x00FF00
    )
    embed.add_field(name="기프트코드", value=f"`{gift_code}`", inline=False)
    embed.add_field(
        name="결과 요약",
        value=f"총 대상: **{len(rows)}**명\n성공: **{success_count}**명 / 실패: **{fail_count}**명",
        inline=False,
    )

    if target_channel:
        await target_channel.send(embed=embed)


# ==========================================
# 📡 디스코드 채널 모니터링 (자동 감지)
# ==========================================
@tasks.loop(seconds=60)
async def monitor_coupon_channel():
    channel = bot.get_channel(MONITOR_CHANNEL_ID)
    report_channel = bot.get_channel(REPORT_CHANNEL_ID)

    if not channel:
        return

    try:
        async for message in channel.history(limit=5):
            found_codes = re.findall(r"\b[A-Z0-9]{6,15}\b", message.content)

            for code in found_codes:
                cursor.execute(
                    "SELECT code FROM used_codes WHERE code = ?", (code,)
                )
                if not cursor.fetchone():
                    await process_mass_redeem(code, report_channel)
    except Exception as e:
        print(f"모니터링 중 에러 발생: {e}")


@monitor_coupon_channel.before_loop
async def before_monitor():
    await bot.wait_until_ready()


# ==========================================
# 💬 슬래시 커맨드 (사용자 & 관리자)
# ==========================================


# 1. 일반 유저 등록
@bot.tree.command(
    name="등록", description="본인의 WOS UID와 서버 번호(왕국)를 등록합니다."
)
@app_commands.describe(uid="플레이어 ID (숫자)", server="왕국 번호 (숫자)")
async def register(interaction: discord.Interaction, uid: str, server: int):
    discord_id = str(interaction.user.id)

    cursor.execute(
        """
        INSERT OR REPLACE INTO users (discord_id, uid, server) VALUES (?, ?, ?)
    """,
        (discord_id, uid, server),
    )
    conn.commit()

    embed = discord.Embed(
        title="✅ 등록 완료",
        description=f"{interaction.user.mention}님의 정보가 성공적으로 저장되었습니다.",
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
    discord_id = str(interaction.user.id)

    cursor.execute(
        "SELECT uid, server FROM users WHERE discord_id = ?", (discord_id,)
    )
    row = cursor.fetchone()

    if row:
        await interaction.response.send_message(
            f"ℹ️ **등록 정보**: UID `{row[0]}` / 왕국 `{row[1]}`번"
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
    cursor.execute("""
        SELECT code, result_summary, used_at 
        FROM used_codes 
        ORDER BY used_at DESC 
        LIMIT 10
    """)
    rows = cursor.fetchall()

    if not rows:
        await interaction.response.send_message(
            "📜 아직 교환 처리된 기프트코드 내역이 없습니다."
        )
        return

    embed = discord.Embed(
        title="📜 최근 기프트코드 교환 히스토리 (최근 10개)",
        color=0x9B59B6,
    )

    for code, summary, used_at in rows:
        date_str = str(used_at).split(".")[0]
        embed.add_field(
            name=f"🎁 코드: `{code}`",
            value=f"└ **결과:** {summary}\n└ **일시:** {date_str}",
            inline=False,
        )

    await interaction.response.send_message(embed=embed)


# 4. 등록 인원 요약
@bot.tree.command(
    name="유저목록",
    description="현재 자동 교환에 등록된 총 유저 수 현황을 확인합니다.",
)
async def user_count(interaction: discord.Interaction):
    cursor.execute("SELECT COUNT(*) FROM users")
    count = cursor.fetchone()[0]

    embed = discord.Embed(
        title="👥 자동 교환 등록 현황",
        description=f"현재 총 **{count}명**의 플레이어가 기프트코드 자동 교환에 등록되어 있습니다.",
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
    discord_id = str(target_user.id)

    cursor.execute(
        "SELECT uid FROM users WHERE discord_id = ?", (discord_id,)
    )
    row = cursor.fetchone()

    if not row:
        await interaction.response.send_message(
            f"❌ {target_user.mention}님은 등록된 정보가 없습니다.",
            ephemeral=True,
        )
        return

    cursor.execute(
        "DELETE FROM users WHERE discord_id = ?", (discord_id,)
    )
    conn.commit()

    embed = discord.Embed(
        title="🗑️ 유저 정보 삭제 완료",
        description=f"{target_user.mention}님의 등록 정보(UID: `{row[0]}`)를 DB에서 삭제했습니다.",
        color=0xE74C3C,
    )
    await interaction.response.send_message(embed=embed)


# 6. [관리자 전용] 등록된 전체 유저 상세 명단 조회
@bot.tree.command(
    name="전체유저목록",
    description="[관리자] DB에 등록된 전체 유저 명단을 조회합니다.",
)
@app_commands.checks.has_permissions(administrator=True)
async def full_user_list(interaction: discord.Interaction):
    cursor.execute("SELECT discord_id, uid, server FROM users")
    rows = cursor.fetchall()

    if not rows:
        await interaction.response.send_message(
            "📋 현재 DB에 등록된 유저가 없습니다.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"📋 등록 유저 명단 (총 {len(rows)}명)", color=0x34495E
    )

    description_text = ""
    for idx, (discord_id, uid, server) in enumerate(rows, 1):
        description_text += (
            f"**{idx}.** <@{discord_id}> | UID: `{uid}` | 왕국: `{server}`번\n"
        )

        if idx % 15 == 0 or idx == len(rows):
            embed.description = description_text
            if idx <= 15:
                await interaction.response.send_message(
                    embed=embed, ephemeral=True
                )
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
            embed = discord.Embed(color=0x34495E)
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
# ⚠️ 관리자 권한 예외 처리 에러 핸들러
# ==========================================
@delete_user.error
@full_user_list.error
@manual_redeem.error
async def admin_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "🚫 이 명령어는 **서버 관리자** 권한이 있는 분만 사용하실 수 있습니다.",
            ephemeral=True,
        )


# ==========================================
# 🚀 메인 실행 부분
# ==========================================
if __name__ == "__main__":
    keep_alive()
    bot.run(BOT_TOKEN)