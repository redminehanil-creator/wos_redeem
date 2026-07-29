import asyncio
import os
import re
import sqlite3
import time
from threading import Thread
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ==========================================
# ⚙️ 설정 구역
# ==========================================
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
# 🔄 셀레니움 기반 기프트코드 웹 자동 교환 로직
# ==========================================
def execute_redeem_api(uid: str, server: int, gift_code: str) -> bool:
    """웹사이트 UI에 접속하여 플레이어 ID, 왕국, 교환 코드를 입력하는 로직"""
    driver = None
    try:
        # 1. 셀레니움 옵션 세팅 (Linux/Render 헤드리스 크롬)
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()), options=options
        )
        wait = WebDriverWait(driver, 10)

        # 2. 교환 센터 접속
        driver.get("https://wos-giftcode.centurygame.com/")
        time.sleep(2)

        # 3. [플레이어 ID] 입력
        id_input = wait.until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//input[@placeholder='플레이어 ID' or contains(@placeholder, 'Player ID')]",
                )
            )
        )
        id_input.clear()
        id_input.send_keys(str(uid))

        # 4. [왕국] 입력
        server_input = driver.find_element(
            By.XPATH,
            "//input[@placeholder='왕국' or contains(@placeholder, 'Kingdom')]",
        )
        server_input.clear()
        server_input.send_keys(str(server))

        # 5. [교환 코드] 입력
        code_input = driver.find_element(
            By.XPATH,
            "//input[contains(@placeholder, '교환 코드를 입력') or contains(@placeholder, 'Gift Code')]",
        )
        code_input.clear()
        code_input.send_keys(str(gift_code))

        # 6. [교환 확인] 버튼 클릭
        confirm_btn = driver.find_element(
            By.XPATH,
            "//button[contains(., '교환 확인') or contains(text(), '교환 확인')]",
        )
        confirm_btn.click()
        time.sleep(2)

        # 7. 성공 여부 확인
        page_source = driver.page_source
        if (
            "성공" in page_source
            or "SUCCESS" in page_source.upper()
            or "발송" in page_source
        ):
            return True
        else:
            print(f"❌ 교환 실패 (UID: {uid}, 서버: {server})")
            return False

    except Exception as e:
        print(f"⚠️ 교환 중 오류 발생 (UID: {uid}): {e}")
        return False
    finally:
        if driver:
            driver.quit()


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
        # 동기 셀레니움 함수를 비동기 루프에서 실행
        loop = asyncio.get_event_loop()
        is_success = await loop.run_in_executor(
            None, execute_redeem_api, uid, server, gift_code
        )

        if is_success:
            success_count += 1
        else:
            fail_count += 1

        if status_msg and (idx % 3 == 0 or idx == len(rows)):
            await status_msg.edit(
                content=f"🔄 **자동 입력 진행 중...** [`{gift_code}`] [{idx}/{len(rows)}]\n✅ 성공: {success_count}명 | ❌ 실패: {fail_count}명"
            )

        await asyncio.sleep(1)

    # 3. 교환 결과를 DB 히스토리에 기록
    summary = f"성공 {success_count}명 / 실패 {fail_count}명"
    cursor.execute(
        """
        INSERT OR REPLACE INTO used_codes (code, result_summary) VALUES (?, ?)
    """,
        (gift_code, summary),
    )
    conn.commit()

    # 4. 완료 임베드 출력
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


@bot.tree.command(
    name="쿠폰발송",
    description="[관리자] 기프트코드를 입력하여 전체 유저에게 일괄 등록합니다.",
)
@app_commands.describe(gift_code="교환할 기프트코드")
async def manual_redeem(interaction: discord.Interaction, gift_code: str):
    await interaction.response.send_message(
        f"⏳ 기프트코드(`{gift_code}`) 수동 발송 작업을 시작합니다..."
    )
    asyncio.create_task(
        process_mass_redeem(gift_code, interaction.channel)
    )


# ==========================================
# 🚀 메인 실행 부분
# ==========================================
if __name__ == "__main__":
    keep_alive()
    bot.run(BOT_TOKEN)