#!/bin/sh
# 認証情報・取得データのコミットを防ぐ（.git/hooks/pre-commit にリンクして使用）
exec python3 scripts/check_secrets.py
