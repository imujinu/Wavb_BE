#!/usr/bin/env python3
"""
Migration 실행 스크립트

사용법:
    python scripts/run_migrations.py
"""

import asyncio
import sys
from pathlib import Path

import asyncpg
from settings import get_settings


async def run_migrations():
    """Migration 파일들을 순서대로 실행"""
    settings = get_settings()

    # migration 파일 디렉토리
    migrations_dir = Path(__file__).parent.parent / "db" / "migrations"

    # migration 파일들 (숫자 순서대로 정렬)
    migration_files = sorted(migrations_dir.glob("*.sql"))

    if not migration_files:
        print("❌ Migration 파일을 찾을 수 없습니다.")
        return False

    print(f"📁 Migration 디렉토리: {migrations_dir}")
    print(f"📋 발견된 migration 파일:")
    for f in migration_files:
        print(f"   - {f.name}")

    # DB 연결
    try:
        print("\n🔗 DB 연결 중...")
        conn = await asyncpg.connect(settings.database_url)
        print("✅ DB 연결 성공")
    except asyncpg.InvalidPasswordError:
        print("❌ DB 암호 오류")
        return False
    except Exception as e:
        print(f"❌ DB 연결 실패: {e}")
        return False

    try:
        # 각 migration 파일 실행
        for migration_file in migration_files:
            print(f"\n▶️  {migration_file.name} 실행 중...")

            try:
                sql = migration_file.read_text()
                await conn.execute(sql)
                print(f"✅ {migration_file.name} 완료")
            except asyncpg.DuplicateTableError:
                print(f"⚠️  {migration_file.name} - 테이블이 이미 존재 (스킵)")
            except asyncpg.DuplicateSchemaError:
                print(f"⚠️  {migration_file.name} - 스키마가 이미 존재 (스킵)")
            except Exception as e:
                print(f"❌ {migration_file.name} 실패: {e}")
                return False

        print("\n✨ 모든 migration 완료!")
        return True

    finally:
        await conn.close()


async def main():
    """메인 함수"""
    success = await run_migrations()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
