import os
import torch
import logging
from typing import Tuple, Optional, Any
# CAMBIO CLAVE: Importamos la clase específica para MoE igual que en tu script V6.8
from transformers import AutoProcessor, Qwen3VLMoeForConditionalGeneration, BitsAndBytesConfig
from pdf2image import convert_from_path
from qwen_vl_utils import process_vision_info

# --- CONFIGURACIÓN DE RUTAS ---
RUTA_PDF = "/home/endless/FUNCIONALIDADES/PROYECTO EXTRACTOR/input/ESTADO DE CUENTA - CONTADORES Y CIA JUNIO 2025.pdf"
RUTA_MODELO = "/home/endless/FUNCIONALIDADES/PRUEBA MODELOS/MODELOS/Qwen3-VL-30B-A3B-Instruct"
RUTA_SALIDA = "/home/endless/FUNCIONALIDADES/PROYECTO EXTRACTOR/PRUEBA_1.txt"

# --- CONFIGURACIÓN DEL LOGGER ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def cargar_modelo() -> Tuple[Optional[Any], Optional[Any]]:
    logger.info(f"Cargando motor Qwen3-VL MoE en RTX 5090 (Directo a GPU)...")
    try:
        # Configuración simplificada
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.float16,  # CAMBIO: float16
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
        )

        # CAMBIO CRÍTICO: Usar Qwen3VLMoeForConditionalGeneration en lugar de AutoModel
        # Esto asegura que se maneje la arquitectura MoE correctamente.
        modelo = Qwen3VLMoeForConditionalGeneration.from_pretrained(
            RUTA_MODELO,
            quantization_config=quantization_config,
            torch_dtype=torch.float16,  # CAMBIO: float16
            device_map={"": 0}, 
            attn_implementation="sdpa",
            trust_remote_code=True,
            low_cpu_mem_usage=True,  # NUEVO: evitar uso de RAM
        )
        
        procesador = AutoProcessor.from_pretrained(
            RUTA_MODELO, 
            trust_remote_code=True,
            max_pixels=768*768,   # CAMBIO: reducido según tu petición
            min_pixels=256*256
        )
        
        logger.info("✓ Modelo Qwen3 cargado con éxito (GPU Direct)")
        return modelo, procesador
    except Exception as e:
        logger.error(f"Error cargando modelo Qwen3: {e}")
        return None, None

def procesar_estado_de_cuenta():
    # 1. Cargar el modelo
    model, processor = cargar_modelo()
    if not model:
        return

    logger.info(f"Procesando PDF: {RUTA_PDF}")

    # 2. Convertir PDF a imágenes (Qwen lee imágenes, no PDFs crudos)
    try:
        images = convert_from_path(RUTA_PDF)
        logger.info(f"PDF convertido en {len(images)} imágenes.")
    except Exception as e:
        logger.error(f"Error al convertir PDF a imágenes: {e}")
        return

    # 3. Preparar el Prompt Específico
    prompt_texto = """
    Analiza este estado de cuenta bancario. Necesito extraer todas las transacciones divididas estrictamente en INGRESOS y EGRESOS.
    
    Reglas de extracción:
    1. Solo extrae 3 campos por transacción: FECHA (formato DD-MMM-AAAA), DESCRIPCION (nombre completo exacto tal cual aparece), MONTO.
    2. No incluyas saldos iniciales, saldos finales ni resúmenes, solo las transacciones individuales.
    3. Respeta el formato de salida exacto abajo.

    Formato de Salida Requerido:
    --- INGRESOS ---
    DD-MMM-AAAA | NOMBRE EXACTO DE LA TRANSACCIÓN | $MONTO
    
    --- EGRESOS ---
    DD-MMM-AAAA | NOMBRE EXACTO DE LA TRANSACCIÓN | $MONTO
    """

    # Construir el mensaje para el modelo (soporta múltiples páginas/imágenes)
    content_list = []
    for img in images:
        content_list.append({"type": "image", "image": img})
    
    content_list.append({"type": "text", "text": prompt_texto})

    messages = [
        {
            "role": "user",
            "content": content_list
        }
    ]

    # 4. Inferencia
    logger.info("Generando extracción de datos...")
    
    text = processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    
    image_inputs, video_inputs = process_vision_info(messages)
    
    inputs = processor(
        text=[text],
        images=image_inputs,
        videos=video_inputs,
        padding=True,
        return_tensors="pt",
    )
    
    inputs = inputs.to("cuda")

    # Generación
    generated_ids = model.generate(**inputs, max_new_tokens=4096)
    
    generated_ids_trimmed = [
        out_ids[len(in_ids) :] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
    ]
    
    output_text = processor.batch_decode(
        generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
    )[0]

    # 5. Guardar resultado
    try:
        with open(RUTA_SALIDA, "w", encoding="utf-8") as f:
            f.write(output_text)
        logger.info(f"✓ Extracción guardada exitosamente en: {RUTA_SALIDA}")
        print("\n--- VISTA PREVIA DEL RESULTADO ---\n")
        print(output_text[:500] + "...\n")
    except Exception as e:
        logger.error(f"Error al guardar el archivo: {e}")

if __name__ == "__main__":
    procesar_estado_de_cuenta()