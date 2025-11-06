#!/bin/bash
# Se ejecutara este script una vez para descargar todos los modelos localmente

# 1. Se instalara Tesseract (Motor OCR de respaldo)
# --- Para Ubuntu/Debian ---
# sudo apt update
# sudo apt install tesseract-ocr tesseract-ocr-spa

# --- Para macOS ---
# brew install tesseract tesseract-lang

# --- Para Windows ---
# Se descargara e instalara desde https://github.com/UB-Mannheim/tesseract/wiki
# Se asegurara de agregar Tesseract al PATH del sistema

# 2. Se pre-descargaran los modelos de PaddleOCR (Motor OCR principal)
# Esto evita descargas en tiempo de ejecucion
python -c "from paddleocr import PaddleOCR; ocr = PaddleOCR(use_angle_cls=True, lang='es', use_gpu=False)"

echo "Instalacion de modelos locales completada."