import argparse
import asyncio
import logging
import os
from pathlib import Path

import discord
from dotenv import load_dotenv

from ai_companion.application.ports import ModelError
from ai_companion.bootstrap import check_model, run_bot
from ai_companion.config import ConfigurationError


def main() -> None:
    parser = argparse.ArgumentParser(description="Discord와 LM Studio 기반 AI Companion")
    parser.add_argument("--env-file", type=Path, help="설정 파일 경로. 기본값: 현재 폴더의 .env")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="허용된 Discord DM을 수신하고 응답")
    check = commands.add_parser("check-model", help="Discord 없이 LM Studio 연결 확인")
    check.add_argument("--prompt", help="지정하면 모델 목록 대신 실제 텍스트 생성 수행")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    # 외부 라이브러리의 HTTP URL·오류 본문을 기본 로그에서 제외한다.
    for name in ("httpx", "httpcore", "discord"):
        external_logger = logging.getLogger(name)
        external_logger.addHandler(logging.NullHandler())
        external_logger.propagate = False
    try:
        if args.env_file is not None and not args.env_file.is_file():
            raise ConfigurationError("지정한 환경 설정 파일이 없습니다.")
        load_dotenv(args.env_file or Path.cwd() / ".env", override=False)
        if args.command == "check-model":
            asyncio.run(check_model(os.environ, args.prompt))
        else:
            asyncio.run(run_bot(os.environ))
    except (ConfigurationError, ModelError) as error:
        parser.exit(1, f"{error}\n")
    except discord.LoginFailure:
        parser.exit(1, "Discord 로그인 실패. 봇 토큰을 확인하세요.\n")
    except (discord.DiscordException, OSError):
        parser.exit(1, "Discord 연결 실패. 네트워크와 봇 설정을 확인하세요.\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
