import asyncio
import os
import re
import sqlite3
from threading import Thread
from discord import app_commands
from discord.ext import commands, tasks
import discord
from flask import Flask
import requests

# ==========================================
# ⚙️ 설정 구역
# ==========================================
# 1단계에서 발급받은 디스코드 봇 토큰을 입력하세요!
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_DISCORD_BOT_TOKEN_HERE")

# 제공해주신 채널 ID 적용 완료
MONITOR_CHANNEL_ID = 973162050333327390  # 모니터링할 채널 ID
REPORT_CHANNEL_ID = 1532160917943484626  # 결과를 보고받을 채널 ID

# ==========================================
# 🌐 Render Web Service 유지용 Flask 가짜 웹 서버
# ==========================================
web_app = Flask("")


@web_app.route("/")
def home():
    return "WOS Discord Bot is Active and Running!"


def run_web():
    # Render가 자동으로 지정해주는 포트를 읽어옵니다 (기본값 8080)
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

# 유저 정보 테이블 (디스코드ID, UID, 서버번호)
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    discord_id TEXT PRIMARY KEY,
    uid TEXT NOT NULL,
    server INTEGER NOT NULL
)
""")

# 사용된 기프트코드 기록 테이블 (중복 처리 방지용)
cursor.execute("""
CREATE TABLE IF NOT EXISTS used_codes (
    code TEXT PRIMARY KEY,
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
        # 슬래시 명령어 동기화
        await self.tree.sync()
        # 자동 모니터링 루프 시작
        monitor_coupon_channel.start()
        print("✅ 슬래시 명령어 동기화 및 모니터링 루프가 시작되었습니다.")


bot = WOSBot()


# ==========================================
# 🔄 기프트코드 교환 핵심 로직
# ==========================================
def execute_redeem_api(uid: str, server: int, gift_code: str) -> bool:
    """실제 WOS 교환 API 호출 함수"""
    url = "https://wos-giftcode-api.centurygame.com/api/gift_code"
    payload = {
        "fid": str(uid),
        "cdk": gift_code,
        "time_zone": "Asia/Seoul",
    }

    try:
        response = requests.post(url, data=payload, timeout=5)
        res_data = response.json()
        # err_code가 0이면 성공
        return res_data.get("err_code") == 0
    except Exception:
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
                f"⚠️ `{gift_code}` 코드는 이미 처리된 코드입니다."
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

    # 코드 사용 기록에 추가
    cursor.execute(
        "INSERT OR IGNORE INTO used_codes (code) VALUES (?)", (gift_code,)
    )
    conn.commit()

    status_msg = None
    if target_channel:
        status_msg = await target_channel.send(
            f"🚀 **새로운 기프트코드 발견!** [`{gift_code}`]\n총 **{len(rows)}명** 교환 작업을 시작합니다."
        )

    success_count = 0
    fail_count = 0

    for idx, (uid, server) in enumerate(rows, 1):
        # API 호출 실행
        is_success = execute_redeem_api(uid, server, gift_code)

        if is_success:
            success_count += 1
        else:
            fail_count += 1

        # 디스코드 차단 및 도배 방지 (5명마다 메시지 갱신)
        if status_msg and (idx % 5 == 0 or idx == len(rows)):
            await status_msg.edit(
                content=f"🔄 **교환 진행 중...** [`{gift_code}`] [{idx}/{len(rows)}]\n✅ 성공: {success_count}명 | ❌ 실패: {fail_count}명"
            )

        await asyncio.sleep(1.2)  # API 서버 IP 차단 방지용 딜레이

    # 완료 임베드 출력
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
@tasks.loop(seconds=60)  # 60초마다 지정된 채널 확인
async def monitor_coupon_channel():
    channel = bot.get_channel(MONITOR_CHANNEL_ID)
    report_channel = bot.get_channel(REPORT_CHANNEL_ID)

    if not channel:
        return

    try:
        # 최근 메시지 5개 확인
        async for message in channel.history(limit=5):
            # 영문 대문자 + 숫자 조합의 6~15자리 코드 패턴 추출
            found_codes = re.findall(r"\b[A-Z0-9]{6,15}\b", message.content)

            for code in found_codes:
                cursor.execute(
                    "SELECT code FROM used_codes WHERE code = ?", (code,)
                )
                if not cursor.fetchone():
                    # 미처리된 새 코드 발견 시 자동으로 일괄 교환 구동!
                    await process_mass_redeem(code, report_channel)
    except Exception as e:
        print(f"모니터링 중 에러 발생: {e}")


@monitor_coupon_channel.before_loop
async def before_monitor():
    await bot.wait_until_ready()


# ==========================================
# 💬 슬래시 커맨드 (사용자 & 관리자)
# ==========================================


# 1. 유저 ID 및 서버 등록
@bot.tree.command(
    name="등록", description="본인의 WOS UID와 서버 번호를 등록합니다."
)
@app_commands.describe(uid="플레이어 ID (숫자)", server="서버 번호 (숫자)")
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
    embed.add_field(name="서버", value=f"{server}번 서버", inline=True)

    await interaction.response.send_message(embed=embed)


# 2. 유저 등록 정보 확인
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
            f"ℹ️ **등록 정보**: UID `{row[0]}` / 서버 `{row[1]}`번"
        )
    else:
        await interaction.response.send_message(
            "❌ 등록된 정보가 없습니다. `/등록 [UID] [서버번호]` 명령어로 등록해주세요."
        )


# 3. 관리자 직접 기프트코드 입력 실행
@bot.tree.command(
    name="쿠폰발송",
    description="[관리자] 기프트코드를 입력하여 전체 유저에게 일괄 등록합니다.",
)
@app_commands.describe(gift_code="교환할 기프트코드")
async def manual_redeem(interaction: discord.Interaction, gift_code: str):
    await interaction.response.send_message(
        f"⏳ 기프트코드(`{gift_code}`) 수동 발송 작업을 시작합니다..."
    )

    # 백그라운드에서 일괄 실행
    asyncio.create_task(
        process_mass_redeem(gift_code, interaction.channel)
    )


# ==========================================
# 🚀 메인 실행 부분
# ==========================================
if __name__ == "__main__":
    keep_alive()  # Render Web Service용 백그라운드 서버 실행
    bot.run(BOT_TOKEN)  # 디스코드 봇 실행