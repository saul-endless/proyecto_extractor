# -*- coding: utf-8 -*-
"""
Parser para estados de cuenta de Banco Inbursa
Version 11.0 - Estandarizado tipo BBVA (v6.0)
- Salida 100% compatible con formato BBVA.
- Usa field_extractors para inteligencia de datos.
- Mantiene lógica de bloques efectiva de Inbursa.
"""

import re
import sys
import os
from datetime import datetime

# Agregar directorio raíz para importar utils
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.field_extractors import (
    funcion_extraer_fecha_normalizada,
    funcion_extraer_monto,
    funcion_extraer_referencia_mejorada,
    funcion_extraer_nombre_completo_transaccion,
    funcion_extraer_beneficiario_correcto,
    funcion_crear_nombre_resumido_inteligente,
    funcion_limpiar_nombre_empresa,
    funcion_formatear_periodo_archivo,
    funcion_determinar_metodo_pago,
    funcion_extraer_cuentas_origen_destino
)

# Se definen los meses en español para conversión rápida local si falla field_extractors
MESES_ESPANOL = {
    'ENE': '01', 'FEB': '02', 'MAR': '03', 'ABR': '04',
    'MAY': '05', 'JUN': '06', 'JUL': '07', 'AGO': '08',
    'SEP': '09', 'OCT': '10', 'NOV': '11', 'DIC': '12'
}

def funcion_extraer_metadatos(texto):
    """
    Extrae metadatos con claves compatibles y robustas.
    """
    metadatos = {
        "Nombre de la empresa del estado de cuenta": "",
        "Numero de cuenta del estado de cuenta": "",
        "Periodo del estado de cuenta": "",
        "Saldo inicial de la cuenta": 0.0,
        "Saldo final de la cuenta": 0.0,
        "Saldo promedio del periodo": 0.0,
        "Cantidad total de depositos": 0.0,
        "Cantidad total de retiros": 0.0,
        "Giro de la empresa": "",
        # Claves auxiliares para compatibilidad interna si fuera necesario
        "nombre_empresa": "",
        "periodo": "",
        "rfc": "" 
    }
    
    # 1. Nombre de la empresa
    # Busca patrones típicos de Inbursa cerca del encabezado
    patron_nombre = r'Página:\s*\d+\s*de\s*\d+\s*\n([A-ZÁÉÍÓÚÑ0-9\s,\.]+(?:SC|SA DE CV|S DE RL|SAPI|CV|A\.C\.|S\.C\.))'
    match_nombre = re.search(patron_nombre, texto)
    if not match_nombre:
        patron_nombre = r'\n([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s,\.]+(?:SC|SA DE CV|S DE RL|SAPI|CV))\s*\n[A-ZÁÉÍÓÚÑ]'
        match_nombre = re.search(patron_nombre, texto)
    
    if match_nombre:
        nombre_raw = match_nombre.group(1).strip()
        metadatos["Nombre de la empresa del estado de cuenta"] = funcion_limpiar_nombre_empresa(nombre_raw)
        metadatos["nombre_empresa"] = nombre_raw

    # 2. Número de cuenta
    match_cuenta = re.search(r'CUENTA\s*\n?\s*(\d{10,12})', texto)
    if match_cuenta:
        metadatos["Numero de cuenta del estado de cuenta"] = match_cuenta.group(1)

    # 3. RFC
    match_rfc = re.search(r'RFC:\s*([A-Z]{3,4}\d{6}[A-Z0-9]{3})', texto)
    if match_rfc:
        metadatos["rfc"] = match_rfc.group(1)

    # 4. Periodo
    # Formato Inbursa: Del 01 Abr. 2025 al 30 Abr. 2025
    match_periodo = re.search(r'Del\s+(\d{1,2})\s+(\w+)\.?\s+(\d{4})\s+al\s+(\d{1,2})\s+(\w+)\.?\s+(\d{4})', texto, re.IGNORECASE)
    if match_periodo:
        d1, m1, a1, d2, m2, a2 = match_periodo.groups()
        mes1 = MESES_ESPANOL.get(m1.upper()[:3], '01')
        mes2 = MESES_ESPANOL.get(m2.upper()[:3], '01')
        periodo_str = f"{d1.zfill(2)}/{mes1}/{a1} AL {d2.zfill(2)}/{mes2}/{a2}"
        
        # Guardar en formato legible y formato archivo
        metadatos["periodo"] = f"DEL {periodo_str.replace(' AL ', ' AL ')}" # Formato compatible con main
        metadatos["Periodo del estado de cuenta"] = funcion_formatear_periodo_archivo(f"{d1}/{mes1}/{a1} {d2}/{mes2}/{a2}")
    
    # 5. Saldos y Totales (Usando funcion_extraer_monto para robustez)
    match_saldo_ini = re.search(r'SALDO ANTERIOR\s*\n?([\d,]+\.\d{2})', texto)
    if match_saldo_ini:
        metadatos["Saldo inicial de la cuenta"] = funcion_extraer_monto(match_saldo_ini.group(1))

    match_saldo_fin = re.search(r'SALDO ACTUAL\s*\n?([\d,]+\.\d{2})', texto)
    if match_saldo_fin:
        metadatos["Saldo final de la cuenta"] = funcion_extraer_monto(match_saldo_fin.group(1))

    match_depositos = re.search(r'ABONOS\s*\n?([\d,]+\.\d{2})', texto)
    if match_depositos:
        metadatos["Cantidad total de depositos"] = funcion_extraer_monto(match_depositos.group(1))

    match_retiros = re.search(r'CARGOS\s*\n?([\d,]+\.\d{2})', texto)
    if match_retiros:
        metadatos["Cantidad total de retiros"] = funcion_extraer_monto(match_retiros.group(1))

    match_promedio = re.search(r'SALDO PROMEDIO\s*\n?([\d,]+\.\d{2})', texto)
    if match_promedio:
        metadatos["Saldo promedio del periodo"] = funcion_extraer_monto(match_promedio.group(1))

    return metadatos

def funcion_extraer_anio_contexto(texto):
    """Extrae el año probable del documento para fechas sin año."""
    match = re.search(r'Del\s+\d+\s+\w+\.?\s+(\d{4})', texto)
    if match:
        return match.group(1)
    return str(datetime.now().year)

def funcion_construir_transaccion_bbva_style(bloque_raw, fecha_str, anio, contador_transacciones, saldo_inicial_tracking):
    """
    Convierte un bloque de texto crudo de Inbursa en un diccionario con formato BBVA.
    """
    # 1. Limpieza y preparación de líneas
    lineas = [l.strip() for l in bloque_raw if l.strip()]
    if not lineas:
        return None, saldo_inicial_tracking

    texto_bloque = ' '.join(lineas).upper()
    
    # 2. Extracción de Montos y Saldos para determinar clasificación
    # Inbursa suele poner: Monto (Cargo/Abono) y luego Saldo
    montos_encontrados = [] # Lista de (tipo_probable, valor)
    
    # Regex específica para líneas de monto en Inbursa (flotando a la derecha o solos)
    for linea in lineas:
        # Busca montos al final de la línea o líneas que son solo montos
        matches = re.findall(r'([\d,]+\.\d{2})', linea)
        for m in matches:
            valor = funcion_extraer_monto(m)
            montos_encontrados.append(valor)

    monto_transaccion = 0.0
    saldo_operacion = 0.0
    es_cargo = True # Default

    if len(montos_encontrados) >= 2:
        # Asumimos que el último es saldo y el penúltimo es el monto
        saldo_operacion = montos_encontrados[-1]
        monto_transaccion = montos_encontrados[-2]
        
        # Determinar si es Ingreso o Egreso basándonos en el saldo anterior
        # Saldo Nuevo = Saldo Anterior + Abono
        # Saldo Nuevo = Saldo Anterior - Cargo
        diff_abono = abs((saldo_inicial_tracking + monto_transaccion) - saldo_operacion)
        diff_cargo = abs((saldo_inicial_tracking - monto_transaccion) - saldo_operacion)
        
        if diff_abono < diff_cargo and diff_abono < 1.0:
            es_cargo = False
        else:
            es_cargo = True
            
        # Actualizar tracking
        saldo_inicial_tracking = saldo_operacion
        
    elif len(montos_encontrados) == 1:
        # Caso raro, solo hay saldo o solo monto. Asumimos saldo si es "BALANCE INICIAL"
        if "BALANCE INICIAL" in texto_bloque:
            saldo_operacion = montos_encontrados[0]
            saldo_inicial_tracking = saldo_operacion
            return None, saldo_inicial_tracking # No es transacción real
        else:
            monto_transaccion = montos_encontrados[0]
            # No podemos actualizar saldo tracking fiablemente sin saldo final
    
    if monto_transaccion == 0 and "BALANCE INICIAL" not in texto_bloque:
        return None, saldo_inicial_tracking

    # 3. Extracción de Datos Específicos (Beneficiario, Referencia, etc.)
    
    # Referencia / Folio
    referencia = funcion_extraer_referencia_mejorada(lineas)
    if not referencia:
        # Inbursa a veces pone la referencia en la primera línea junto a la fecha (que ya se parseó fuera)
        pass 

    # Buscar "Clave de Rastreo" (Típico Inbursa) para agregarla a referencia si hace falta
    match_rastreo = re.search(r'(?:CLAVE DE RASTREO|RASTREO)\s*[:\.]?\s*([A-Z0-9]+)', texto_bloque)
    clave_rastreo = match_rastreo.group(1) if match_rastreo else ""
    
    # Código simulado para compatibilidad con funciones BBVA
    codigo_ficticio = "T17" if "SPEI" in texto_bloque else "A15" # Solo para ayudar a heurísticas
    
    # Beneficiario
    # Inbursa a veces etiqueta explícitamente o pone el nombre después del concepto
    beneficiario = ""
    match_ben_explicit = re.search(r'(?:BENEFICIARIO|ORDENANTE)\s*[:\.]?\s*([A-Z\s\.,&]+)', texto_bloque)
    if match_ben_explicit:
        beneficiario = match_ben_explicit.group(1).strip()
    else:
        # Usar la función inteligente de field_extractors
        beneficiario = funcion_extraer_beneficiario_correcto(lineas, codigo_ficticio, es_cargo)
    
    # Nombre de la transacción (Concepto completo)
    # Quitamos montos y palabras clave irrelevantes para limpiar
    nombre_transaccion = funcion_extraer_nombre_completo_transaccion(lineas, -1, lineas[0]) # Usamos todo el bloque

    # Tipo de Transacción
    # Mapeo manual mejorado para Inbursa
    tipo_transaccion = "Otro"
    if "SPEI" in texto_bloque: tipo_transaccion = "Transferencia"
    elif "DEPOSITO" in texto_bloque: tipo_transaccion = "Depósito"
    elif "CHEQUE" in texto_bloque: tipo_transaccion = "Cheque"
    elif "COMISION" in texto_bloque: tipo_transaccion = "Comisión"
    elif "IVA" in texto_bloque and "COMISION" in texto_bloque: tipo_transaccion = "Comisión"
    elif "INTERESES" in texto_bloque: tipo_transaccion = "Depósito" # O Interés
    elif "ISR" in texto_bloque: tipo_transaccion = "Impuesto"
    elif "TRASPASO" in texto_bloque: tipo_transaccion = "Transferencia"
    elif "CARGO" in texto_bloque: tipo_transaccion = "Cargo"
    
    # Clasificación
    clasificacion = "Egreso" if es_cargo else "Ingreso"

    # Método de Pago
    metodo_pago = funcion_determinar_metodo_pago(codigo_ficticio, nombre_transaccion)
    
    # Cuentas
    cuenta_origen, cuenta_destino = funcion_extraer_cuentas_origen_destino(lineas, es_cargo, "")
    
    # Nombre Resumido Inteligente
    nombre_resumido = funcion_crear_nombre_resumido_inteligente(
        nombre_transaccion, tipo_transaccion, beneficiario, contador_transacciones
    )

    # Fecha formateada (La fecha viene del bloque externo, hay que normalizarla)
    # fecha_str suele ser "DD/MM" o similar. Le agregamos el año.
    fecha_final = fecha_str
    if len(fecha_str) <= 5: # Es DD/MM o similar
        if "/" in fecha_str:
            fecha_final = f"{fecha_str}/{anio}"
    
    # Validar formato DD/MM/AAAA con la función útil
    fecha_final = funcion_extraer_fecha_normalizada(fecha_final)

    # Construcción del diccionario FINAL ESTILO BBVA
    transaccion = {
        "Fecha de la transacción": fecha_final,
        "Nombre de la transacción": nombre_transaccion,
        "Nombre resumido": nombre_resumido,
        "Tipo de transacción": tipo_transaccion,
        "Clasificación": clasificacion,
        "Quien realiza o recibe el pago": beneficiario,
        "Monto de la transacción": monto_transaccion,
        "Numero de referencia o folio": referencia or clave_rastreo, # Preferencia a ref corta, fallback a rastreo
        "Numero de cuenta origen": cuenta_origen,
        "Numero de cuenta destino": cuenta_destino,
        "Metodo de pago": metodo_pago,
        "Sucursal o ubicacion": "", # Inbursa raramente trae sucursal en linea
        
        # Campos vacíos requeridos
        "Giro de la transacción": "",
        "Giro sugerido": "",
        "Análisis monto": "",
        "Análisis contraparte": "",
        "Análisis naturaleza": ""
    }

    return transaccion, saldo_inicial_tracking

def funcion_extraer_transacciones_inbursa_core(texto, saldo_inicial):
    """
    Motor de extracción que itera sobre el texto y detecta bloques de fecha.
    """
    lineas = texto.split('\n')
    transacciones = []
    contador_transacciones = {}
    
    anio = funcion_extraer_anio_contexto(texto)
    saldo_tracking = saldo_inicial
    
    # Regex para detectar inicio de transacción: Mes abreviado + Dia (Ej: ENE 01, ABR. 30)
    patron_inicio = re.compile(r'^(ENE|FEB|MAR|ABR|MAY|JUN|JUL|AGO|SEP|OCT|NOV|DIC)\.?\s*(\d{1,2})', re.IGNORECASE)
    
    bloque_actual = []
    fecha_actual = ""
    
    for linea in lineas:
        linea_limpia = linea.strip()
        match_fecha = patron_inicio.match(linea_limpia)
        
        # Ignorar encabezados recurrentes dentro del flujo
        if any(x in linea_limpia.upper() for x in ["SALDO ANTERIOR", "SALDO ACTUAL", "PÁGINA", "ESTADO DE CUENTA"]):
             if not match_fecha: continue

        if match_fecha:
            # Procesar bloque anterior si existe
            if bloque_actual and fecha_actual:
                tx, saldo_tracking = funcion_construir_transaccion_bbva_style(
                    bloque_actual, fecha_actual, anio, contador_transacciones, saldo_tracking
                )
                if tx: transacciones.append(tx)
            
            # Iniciar nuevo bloque
            mes_str = match_fecha.group(1).upper()
            dia_str = match_fecha.group(2).zfill(2)
            mes_num = MESES_ESPANOL.get(mes_str.replace('.', ''), '01')
            fecha_actual = f"{dia_str}/{mes_num}"
            
            bloque_actual = [linea_limpia] # Incluimos la primera línea que trae descripción a veces
        else:
            if bloque_actual:
                bloque_actual.append(linea_limpia)
                
    # Procesar último bloque
    if bloque_actual and fecha_actual:
        tx, saldo_tracking = funcion_construir_transaccion_bbva_style(
            bloque_actual, fecha_actual, anio, contador_transacciones, saldo_tracking
        )
        if tx: transacciones.append(tx)
        
    return transacciones

# =============================================================================
# FUNCIONES PÚBLICAS REQUERIDAS POR main_extractor.py
# =============================================================================

def parsear_datos_generales(paginas_texto: list) -> dict:
    """
    Punto de entrada 1: Extrae metadatos.
    Main espera un dict con claves como 'saldo_inicial' para pasar al siguiente paso.
    """
    texto_completo = "\n".join(paginas_texto)
    return funcion_extraer_metadatos(texto_completo)

def parsear_transacciones(paginas_texto: list, saldo_inicial: float) -> list:
    """
    Punto de entrada 2: Extrae transacciones.
    Main espera una lista de diccionarios.
    """
    texto_completo = "\n".join(paginas_texto)
    print(f"   > Iniciando extracción detallada Inbursa (v11.0)... Saldo Inicial Ref: {saldo_inicial}")
    return funcion_extraer_transacciones_inbursa_core(texto_completo, saldo_inicial)

# =============================================================================
# COMPATIBILIDAD DIRECTA
# =============================================================================
def parse(texto: str) -> dict:
    return parsear_datos_generales([texto])