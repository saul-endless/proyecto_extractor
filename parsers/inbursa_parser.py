# -*- coding: utf-8 -*-
"""
Parser Inbursa Empresarial v6.0 - CORREGIDO Y ROBUSTO
Soluciona:
1. Fechas rotas por OCR (01 \n ABR.)
2. Extracción de tablas de impuestos (CFDI) como transacciones falsas.
3. Identificación incorrecta de Empresa (confundir con RESUMEN DE SALDOS).
4. Clasificación Ingreso/Egreso basada en matemáticas de saldos.
"""

import re
import sys
import os

# Importación de utilidades
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.field_extractors import (
    funcion_extraer_fecha_normalizada,
    funcion_extraer_monto,
    funcion_extraer_referencia_mejorada,
    funcion_extraer_nombre_completo_transaccion,
    funcion_extraer_beneficiario_correcto,
    funcion_crear_nombre_resumido_inteligente,
    funcion_extraer_saldo_promedio,
    funcion_limpiar_nombre_empresa,
    funcion_formatear_periodo_archivo,
    funcion_determinar_metodo_pago,
    funcion_extraer_cuentas_origen_destino
)

# ==============================================================================
# INTERFAZ PÚBLICA (Requerida por main_extractor)
# ==============================================================================

def parsear_datos_generales(text_input):
    """Interfaz para metadatos."""
    texto_completo = _normalizar_entrada(text_input)
    # IMPORTANTE: Normalizar fechas rotas antes de procesar
    texto_procesado = funcion_normalizar_texto_inbursa(texto_completo)
    print("--- Extrayendo Metadatos Inbursa v6.0 ---")
    return funcion_extraer_metadatos_completos(texto_procesado)

def parsear_transacciones(text_input, saldo_inicial=None):
    """Interfaz para transacciones."""
    texto_completo = _normalizar_entrada(text_input)
    texto_procesado = funcion_normalizar_texto_inbursa(texto_completo)
    
    print("--- Extrayendo Transacciones Inbursa v6.0 ---")
    metadatos = funcion_extraer_metadatos_completos(texto_procesado)
    
    # Priorizar saldo del main si existe, sino el del PDF
    if saldo_inicial is not None:
        metadatos['saldo_inicial'] = saldo_inicial
        
    return funcion_extraer_todas_transacciones(texto_procesado, metadatos)

def _normalizar_entrada(text_input):
    if isinstance(text_input, list):
        return "\n".join(text_input)
    return str(text_input)

# ==============================================================================
# LÓGICA CORE DE PARSEO
# ==============================================================================

def funcion_normalizar_texto_inbursa(texto):
    """
    ARREGLA EL PDF: Une fechas que el OCR partió en dos líneas.
    Ej: "ABR.\n01" -> "01 ABR."
    """
    texto_out = texto
    
    # 1. Caso: Mes arriba, Día abajo (Ej: "ABR.\n01")
    # Busca 3 letras mayus/minus + punto opcional + salto + 1 o 2 digitos
    patron_mes_dia = re.compile(r'\b([A-Za-z]{3}\.?)\s*\n+\s*(\d{1,2})\b', re.MULTILINE)
    texto_out = patron_mes_dia.sub(r'\2 \1', texto_out)

    # 2. Caso: Día arriba, Mes abajo (Ej: "01\nABR.")
    patron_dia_mes = re.compile(r'\b(\d{1,2})\s*\n+\s*([A-Za-z]{3}\.?)', re.MULTILINE)
    texto_out = patron_dia_mes.sub(r'\1 \2', texto_out)

    # 3. Limpieza de líneas basura que confunden al parser
    lineas_limpias = []
    for linea in texto_out.split('\n'):
        l = linea.strip()
        if "The following table" in l: continue
        if re.match(r'^,+$', l): continue 
        if not l: continue
        lineas_limpias.append(linea)
        
    return "\n".join(lineas_limpias)

def funcion_extraer_metadatos_completos(texto):
    """
    Extrae metadatos con regex ajustada para Inbursa (case insensitive, puntos en meses).
    """
    datos = {
        'nombre_empresa': '',
        'periodo': '',
        'numero_cuenta': '',
        'saldo_inicial': 0.0,
        'saldo_final': 0.0,
        'total_depositos': 0.0,
        'total_retiros': 0.0,
        'rfc': '',
        'saldo_promedio': 0.0
    }
    
    lineas = texto.split('\n')
    
    # 1. Nombre Empresa: Busca SC, SA DE CV, pero ignora "RESUMEN DE SALDOS"
    for linea in lineas[:50]:
        if re.search(r'(S\.?C\.?|S\.?A\.?|LTD|INC|ASOCIACION|GRUPO|CONTADORES)', linea, re.IGNORECASE):
            # Filtros de exclusión estrictos
            upper_line = linea.upper()
            if any(x in upper_line for x in ['INBURSA', 'BANCO', 'RESUMEN', 'SALDOS', 'ESTADO DE CUENTA']):
                continue
            
            datos['nombre_empresa'] = linea.strip()
            break
            
    # 2. Número de Cuenta
    match_cuenta = re.search(r'(?:Cuenta|CUENTA|Contrato)[\s\.:]+(\d{10,12})', texto)
    if match_cuenta:
        datos['numero_cuenta'] = match_cuenta.group(1)

    # 3. RFC
    match_rfc = re.search(r'RFC[:\s]+([A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3})', texto, re.IGNORECASE)
    if match_rfc:
        datos['rfc'] = match_rfc.group(1).upper()

    # 4. Periodo (Maneja "Abr." con punto y mayúscula inicial)
    # Regex flexible: Del (dia) (mes con/sin punto) (año) al ...
    match_periodo = re.search(r'Del\s+(\d{1,2})\s+([A-Za-z]{3}\.?)\s+(\d{4})\s+al\s+(\d{1,2})\s+([A-Za-z]{3}\.?)\s+(\d{4})', texto, re.IGNORECASE)
    if match_periodo:
        # Reconstruir string limpio: "DEL 01 ABR 2025 AL 30 ABR 2025"
        ini_d, ini_m, ini_y = match_periodo.group(1), match_periodo.group(2), match_periodo.group(3)
        fin_d, fin_m, fin_y = match_periodo.group(4), match_periodo.group(5), match_periodo.group(6)
        
        datos['periodo'] = f"DEL {ini_d} {ini_m.replace('.','')} {ini_y} AL {fin_d} {fin_m.replace('.','')} {fin_y}".upper()
        # Guardar año para uso en transacciones
        datos['_anio_aux'] = ini_y 
    else:
        # Fallback
        fechas = re.findall(r'(\d{1,2}\s+[A-Za-z]{3}\.?\s+\d{4})', texto[:1500])
        if len(fechas) >= 2:
            datos['periodo'] = f"DEL {fechas[0]} AL {fechas[-1]}".upper()

    # 5. Saldos y Totales (Búsqueda multilínea)
    match_saldo_ant = re.search(r'(?:SALDO ANTERIOR|Saldo Inicial).*?([\d,]+\.\d{2})', texto, re.IGNORECASE | re.DOTALL)
    if match_saldo_ant: datos['saldo_inicial'] = funcion_extraer_monto(match_saldo_ant.group(1))

    match_abonos = re.search(r'(?:ABONOS|Depósitos).*?([\d,]+\.\d{2})', texto, re.IGNORECASE | re.DOTALL)
    if match_abonos: datos['total_depositos'] = funcion_extraer_monto(match_abonos.group(1))

    match_cargos = re.search(r'(?:CARGOS|Retiros).*?([\d,]+\.\d{2})', texto, re.IGNORECASE | re.DOTALL)
    if match_cargos: datos['total_retiros'] = funcion_extraer_monto(match_cargos.group(1))

    match_saldo_fin = re.search(r'(?:SALDO ACTUAL|Saldo Final).*?([\d,]+\.\d{2})', texto, re.IGNORECASE | re.DOTALL)
    if match_saldo_fin: datos['saldo_final'] = funcion_extraer_monto(match_saldo_fin.group(1))

    datos['saldo_promedio'] = funcion_extraer_saldo_promedio(texto)

    return {
        "Nombre de la empresa del estado de cuenta": funcion_limpiar_nombre_empresa(datos['nombre_empresa']),
        "Numero de cuenta del estado de cuenta": datos['numero_cuenta'],
        "Periodo del estado de cuenta": funcion_formatear_periodo_archivo(datos['periodo']),
        "Saldo inicial de la cuenta": datos['saldo_inicial'],
        "Saldo final de la cuenta": datos['saldo_final'],
        "Saldo promedio del periodo": datos['saldo_promedio'],
        "Cantidad total de depositos": datos['total_depositos'],
        "Cantidad total de retiros": datos['total_retiros'],
        "Giro de la empresa": "",
        "RFC": datos['rfc'],
        "_anio_aux": datos.get('_anio_aux', '2025') # Default fallback
    }

def funcion_extraer_todas_transacciones(texto, metadatos):
    """
    Extrae transacciones deteniéndose antes de las tablas del SAT.
    """
    # 1. Encontrar inicio real de movimientos
    inicio_movs = -1
    for key in ["DETALLE DE MOVIMIENTOS", "MOVIMIENTOS DEL PERIODO", "FECHA REFERENCIA CONCEPTO"]:
        match = re.search(key, texto, re.IGNORECASE)
        if match:
            inicio_movs = match.start()
            break
            
    if inicio_movs == -1: return []

    # 2. Encontrar fin real (STOP WORDS) para evitar basura del SAT
    fin_movs = len(texto)
    # Estas frases indican que se acabó el estado de cuenta y empieza lo fiscal
    stop_words = ["RESUMEN DEL CFDI", "TIMBRE FISCAL", "SELLO DIGITAL", "CADENA ORIGINAL", "GLOSARIO DE ABREVIATURAS"]
    
    for word in stop_words:
        match_fin = re.search(word, texto[inicio_movs:], re.IGNORECASE)
        if match_fin:
            posible_fin = inicio_movs + match_fin.start()
            if posible_fin < fin_movs:
                fin_movs = posible_fin

    texto_seccion = texto[inicio_movs:fin_movs]
    lineas = texto_seccion.split('\n')
    
    transacciones = []
    bloque_actual = []
    anio_base = metadatos.get('_anio_aux', '2025')
    
    # Regex estricta para inicio de transacción (gracias a la normalización, ahora están en una línea)
    # Ej: "01 ABR" o "01 ABR."
    patron_inicio_tx = re.compile(r'^\s*\d{1,2}\s+[A-Za-z]{3}\.?', re.IGNORECASE)
    
    for linea in lineas:
        if patron_inicio_tx.match(linea):
            # Procesar bloque anterior
            if bloque_actual:
                tx = funcion_procesar_bloque_transaccion(bloque_actual, metadatos, anio_base, transacciones)
                if tx: transacciones.append(tx)
            bloque_actual = [linea]
        else:
            if bloque_actual:
                bloque_actual.append(linea)
                
    # Procesar último bloque
    if bloque_actual:
        tx = funcion_procesar_bloque_transaccion(bloque_actual, metadatos, anio_base, transacciones)
        if tx: transacciones.append(tx)
        
    return transacciones

def funcion_procesar_bloque_transaccion(lineas, metadatos, anio, transacciones_previas):
    """
    Procesa un bloque de texto correspondiente a un movimiento.
    Usa validación matemática para determinar clasificación.
    """
    texto_bloque = " ".join(lineas)
    
    # Ignorar "BALANCE INICIAL" si es solo una línea de saldo arrastrado
    if "BALANCE INICIAL" in texto_bloque.upper() and len(lineas) < 3:
        # A veces es mejor extraerlo como referencia de saldo inicial, pero no es transacción.
        # Lo marcamos como null para saltarlo o lo procesamos con cuidado.
        pass

    # 1. Extraer Fecha
    match_fecha = re.match(r'^\s*(\d{1,2})\s+([A-Za-z]{3}\.?)', lineas[0], re.IGNORECASE)
    if not match_fecha: return None
    
    dia = match_fecha.group(1).zfill(2)
    mes_str = match_fecha.group(2).replace('.', '').upper()
    
    mapa_meses = {'ENE':'01', 'FEB':'02', 'MAR':'03', 'ABR':'04', 'MAY':'05', 'JUN':'06',
                  'JUL':'07', 'AGO':'08', 'SEP':'09', 'OCT':'10', 'NOV':'11', 'DIC':'12'}
    mes_num = mapa_meses.get(mes_str, '00')
    if mes_num == '00': mes_num = mapa_meses.get(mes_str[:3], '00') # Intento con 3 letras
    
    fecha_fmt = f"{dia}/{mes_num}/{anio}"
    
    # 2. Extraer Montos
    # Buscamos todos los montos formato 1,234.56
    montos_strs = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', texto_bloque)
    
    monto_tx = 0.0
    saldo_momento = 0.0
    
    if len(montos_strs) >= 2:
        # Asunción Inbursa: [Monto Operación] ...texto... [Saldo Final]
        saldo_momento = funcion_extraer_monto(montos_strs[-1])
        monto_tx = funcion_extraer_monto(montos_strs[-2])
    elif len(montos_strs) == 1:
        monto_tx = funcion_extraer_monto(montos_strs[0])
        saldo_momento = 0.0

    if monto_tx == 0: return None

    # 3. Clasificación Matemática (Ingreso vs Egreso)
    es_cargo = True # Default
    
    # Obtener saldo anterior
    if transacciones_previas:
        saldo_previo = transacciones_previas[-1].get('_saldo_calculado', 0.0)
    else:
        saldo_previo = metadatos.get('saldo_inicial', 0.0)
    
    if saldo_previo > 0 and saldo_momento > 0:
        # Matemáticas
        diff_cargo = abs(saldo_previo - monto_tx - saldo_momento) # Saldo - Monto = Nuevo
        diff_abono = abs(saldo_previo + monto_tx - saldo_momento) # Saldo + Monto = Nuevo
        
        if diff_cargo < 1.0:
            es_cargo = True
        elif diff_abono < 1.0:
            es_cargo = False
        else:
            # Fallback a texto si la matemática no cuadra (fechas desordenadas)
            es_cargo = funcion_clasificar_por_texto(texto_bloque)
    else:
        # Fallback a texto si no hay saldos
        es_cargo = funcion_clasificar_por_texto(texto_bloque)

    clasificacion = "Egreso" if es_cargo else "Ingreso"

    # 4. Limpieza de Descripción y Referencia
    desc_raw = texto_bloque
    # Quitar fecha inicial
    desc_raw = desc_raw[match_fecha.end():].strip()
    # Quitar montos encontrados
    for m in montos_strs:
        desc_raw = desc_raw.replace(m, "")
    desc_raw = re.sub(r'\s+', ' ', desc_raw).strip()
    
    referencia = ""
    # Inbursa pone la referencia a menudo como el primer número largo al inicio de la descripción
    match_ref = re.match(r'^(\d{5,20})\s', desc_raw)
    if match_ref:
        referencia = match_ref.group(1)
        desc_raw = desc_raw[match_ref.end():].strip()
    else:
        # Buscar palabras clave BNET, REF, FOLIO
        match_bnet = re.search(r'\b(BNET\w+|REF\s*\d+|FOLIO\s*\d+)', desc_raw)
        if match_bnet:
            referencia = match_bnet.group(1)

    # 5. Extracción campos finales
    nombre_completo = funcion_extraer_nombre_completo_transaccion(lineas, 0, desc_raw)
    beneficiario = funcion_extraer_beneficiario_correcto(lineas, "SPEI", es_cargo)
    
    cta_origen, cta_destino = funcion_extraer_cuentas_origen_destino(
        lineas, es_cargo, metadatos.get('numero_cuenta', '')
    )
    
    tipo_tx = funcion_determinar_tipo_transaccion(desc_raw)
    metodo_pago = funcion_determinar_metodo_pago("GENERICO", desc_raw)
    
    nombre_resumido = funcion_crear_nombre_resumido_inteligente(
        nombre_completo, tipo_tx, beneficiario, {}
    )

    return {
        "Fecha de la transacción": fecha_fmt,
        "Nombre de la transacción": nombre_completo,
        "Nombre resumido": nombre_resumido,
        "Tipo de transacción": tipo_tx,
        "Clasificación": clasificacion,
        "Quien realiza o recibe el pago": beneficiario,
        "Monto de la transacción": monto_tx,
        "Numero de referencia o folio": referencia,
        "Numero de cuenta origen": cta_origen,
        "Numero de cuenta destino": cta_destino,
        "Metodo de pago": metodo_pago,
        "Sucursal o ubicacion": "",
        "Giro de la transacción": "",
        "Giro sugerido": "",
        "Análisis monto": "",
        "Análisis contraparte": "",
        "Análisis naturaleza": "",
        # Campo auxiliar para el cálculo del siguiente saldo
        "_saldo_calculado": saldo_momento if saldo_momento > 0 else (saldo_previo - monto_tx if es_cargo else saldo_previo + monto_tx)
    }

def funcion_clasificar_por_texto(texto):
    """Heurística basada en keywords cuando falla la matemática."""
    t = texto.upper()
    if any(x in t for x in ['DEPOSITO', 'ABONO', 'RECEPCION', 'INTERESES GANADOS', 'TRASPASO DE TERCEROS']):
        return False # Ingreso
    return True # Egreso (por defecto más seguro)

def funcion_determinar_tipo_transaccion(texto):
    t = texto.upper()
    if 'SPEI' in t or 'TRASPASO' in t: return 'Transferencia'
    if 'DEPOSITO' in t: return 'Depósito'
    if 'CHEQUE' in t: return 'Cheque'
    if 'COMISION' in t or 'MANEJO DE CUENTA' in t: return 'Comisión'
    if 'IVA' in t or 'ISR' in t: return 'Impuesto'
    if 'INTERES' in t: return 'Rendimiento'
    return 'Otro'

def funcion_validar_balance_transacciones(transacciones, metadatos):
    ing = sum(t['Monto de la transacción'] for t in transacciones if t['Clasificación'] == 'Ingreso')
    egr = sum(t['Monto de la transacción'] for t in transacciones if t['Clasificación'] == 'Egreso')
    print(f"✓ Balance Calculado: Ingresos ${ing:,.2f} | Egresos ${egr:,.2f}")
    
    # Limpieza final
    for t in transacciones:
        if '_saldo_calculado' in t: del t['_saldo_calculado']