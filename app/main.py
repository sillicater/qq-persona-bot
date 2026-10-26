import asyncio
import logging
import sys

from . import db, memes, memory, schedule, stickers
from .bot import Bot
from .config import Config
from .llm import LLM
from .persona import Persona


async def amain():
    cfg = Config.load()
    if not cfg.api_key:
        print("缺少 DEEPSEEK_API_KEY，检查 .env", file=sys.stderr)
        sys.exit(2)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db.init(cfg.data_dir / "bot.db")
    memory.set_archive_path(cfg.data_dir / "档案.md")
    memes.set_path(cfg.persona_path.parent / "memes.yaml")
    stickers.set_path(cfg.persona_path.parent / "stickers.json")
    memory.seed_profile(cfg.persona_path.parent / "profile_seed.yaml")
    memory.export_profile()
    schedule.load_dates(cfg.persona_path.parent / "dates.yaml")
    persona = Persona(cfg.persona_path)
    llm = LLM(cfg)
    bot = Bot(cfg, persona, llm)
    await bot.run()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
