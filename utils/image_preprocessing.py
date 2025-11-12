# -*- coding: utf-8 -*-
"""
Modulo de preprocesamiento de imagenes para mejorar OCR.
"""

import cv2
import numpy as np
from PIL import Image
import fitz


def preprocess_page_for_ocr(pdf_page, zoom_factor=2.5):
    """Convierte pagina PDF a imagen de alta calidad para OCR."""
    mat = fitz.Matrix(zoom_factor, zoom_factor)
    pix = pdf_page.get_pixmap(matrix=mat, alpha=False)
    
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    
    if pix.n == 3:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    elif pix.n == 4:
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2GRAY)
    
    return img


def apply_advanced_preprocessing(image):
    """Aplica tecnicas avanzadas de preprocesamiento."""
    _, binary = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    
    denoised = cv2.fastNlMeansDenoising(binary, h=10)
    
    deskewed = deskew_image(denoised)
    
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    morphed = cv2.morphologyEx(deskewed, cv2.MORPH_CLOSE, kernel)
    
    return morphed


def deskew_image(image):
    """Corrige la inclinacion de la imagen."""
    coords = np.column_stack(np.where(image > 0))
    
    if len(coords) < 100:
        return image
    
    angle = cv2.minAreaRect(coords)[-1]
    
    if angle < -45:
        angle = 90 + angle
    
    if abs(angle) > 0.5:
        (h, w) = image.shape[:2]
        center = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(
            image, M, (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE
        )
        return rotated
    
    return image


def enhance_table_detection(image):
    """Mejora la deteccion de tablas bancarias."""
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    horizontal_lines = cv2.morphologyEx(image, cv2.MORPH_OPEN, horizontal_kernel)
    
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
    vertical_lines = cv2.morphologyEx(image, cv2.MORPH_OPEN, vertical_kernel)
    
    enhanced = cv2.addWeighted(image, 0.7, horizontal_lines, 0.15, 0)
    enhanced = cv2.addWeighted(enhanced, 0.85, vertical_lines, 0.15, 0)
    
    return enhanced


def prepare_image_for_ocr(pdf_page, enhance_tables=True):
    """Pipeline completo de preprocesamiento para OCR."""
    img = preprocess_page_for_ocr(pdf_page, zoom_factor=2.5)
    
    processed = apply_advanced_preprocessing(img)
    
    if enhance_tables:
        processed = enhance_table_detection(processed)
    
    if len(processed.shape) == 2:
        processed = cv2.cvtColor(processed, cv2.COLOR_GRAY2RGB)
    
    return processed


def save_preprocessed_image(image, output_path):
    """Guarda imagen preprocesada para debugging."""
    cv2.imwrite(output_path, image)
    print(f"Imagen preprocesada guardada en: {output_path}")