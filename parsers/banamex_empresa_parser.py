# -*- coding: utf-8 -*-
"""
Parser Banamex Empresa v9.0 (Estándar BBVA Unificado)
- Estandarización TOTAL con BBVA usando las mismas funciones de utilidad.
- Normalización idéntica de fechas, montos, nombres y referencias.
- Extracción inteligente de beneficiarios y conceptos.
"""

import re
import sys
import os

# Asegurar importación de utilidades compartidas (IGUAL QUE BBVA)
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
    funcion_extraer_cuentas_origen_destino,
    funcion_es_codigo_cargo
)
from utils.validators import limpiar_monto

# =============================================================================
# PATRONES ESPECÍFICOS DE BANAMEX
# =============================================================================
PATRONES_SALDO_FINAL = [
    r'SALDO AL \d{2} DE [A-Z]+ DE \d{4}\s+([$]?[\d,]+\.\d{2})',
    r'SALDO AL CORTE.*?([$]?[\d,]+\.\d{2})',
    r'SALDO AL \d{2}/[A-Z]{3}/\d{4}.*?([$]?[\d,]+\.\d{2})'
]

def funcion_parsear_datos_generales(paginas_texto):
    """Función de compatibilidad."""
    texto_completo = "\n".join(paginas_texto)
    return funcion_extraer_metadatos_completos(texto_completo)

def funcion_parsear_transacciones(paginas_texto, saldo_inicial):
    """Función de compatibilidad."""
    texto_completo = "\n".join(paginas_texto)
    metadatos = funcion_extraer_metadatos_completos(texto_completo)
    return funcion_extraer_todas_transacciones(texto_completo, metadatos)

def funcion_parsear_banamex_empresa(texto_completo):
    """
    Parser principal Banamex Empresa.
    Estructura idéntica al parser de BBVA.
    """
    print("\n=== Iniciando Parser Banamex Empresa v9.0 (Estándar BBVA) ===")
    
    metadatos = funcion_extraer_metadatos_completos(texto_completo)
    
    nombre = metadatos.get("Nombre de la empresa del estado de cuenta", "DESCONOCIDO")
    periodo = metadatos.get("Periodo del estado de cuenta", "DESCONOCIDO")
    print(f"✓ Metadatos extraídos: '{nombre}'")
    print(f"✓ Período detectado: '{periodo}'")
    
    transacciones = funcion_extraer_todas_transacciones(texto_completo, metadatos)
    print(f"✓ Transacciones extraídas: {len(transacciones)}")
    
    funcion_validar_balance_transacciones(transacciones, metadatos)
    
    return {
        'datos_generales': metadatos,
        'transacciones': transacciones
    }

def funcion_extraer_metadatos_completos(texto):
    """
    Extrae metadatos generales usando las mismas funciones de limpieza que BBVA.
    """
    datos = {
        'nombre_empresa': '',
        'periodo': '',
        'numero_cuenta_clabe': '',
        'numero_cuenta': '',
        'saldo_inicial': 0.0,
        'total_depositos': 0.0,
        'total_retiros': 0.0,
        'saldo_final': 0.0,
        'saldo_promedio': 0.0,
        'sucursal': ''
    }
    
    lineas = texto.split('\n')
    
    # 1. Nombre Empresa (Lógica robusta)
    candidatos_nombre = []
    for linea in lineas[:60]:
        l = linea.strip()
        if len(l) < 5: continue
        
        # Filtros de exclusión comunes
        if any(x in l.upper() for x in ['BANAMEX', 'SUCURSAL', 'RFC', 'CLIENTE', 'PÁGINA', 'ESTADO DE CUENTA', 'ACTUARIO', 'SANTA FE', 'COL.', 'C.P.', 'CIUDAD DE MEXICO', 'CALLE', 'AVENIDA', 'TORRE']): 
            continue
        
        # Prioridad a razones sociales
        if re.search(r'\b(SA DE CV|S\.A\.|S\.C\.|SOCIEDAD|ASOCIACION|GRUPO|CORPORATIVO|INMOVITUR|SC DE RL|S\.A\.B\.)\b', l, re.IGNORECASE):
            datos['nombre_empresa'] = l
            break
        
        if l.isupper() and not re.search(r'\d', l):
            candidatos_nombre.append(l)
            
    if not datos['nombre_empresa'] and candidatos_nombre:
        datos['nombre_empresa'] = candidatos_nombre[0]

    # 2. Periodo (Normalización avanzada para Banamex)
    match_rango = re.search(r'(?:RESUMEN|PERIODO).*?(\d{2})[/. ]([A-Z]{3})[/. ](\d{4})\s+AL\s+(\d{2})[/. ]([A-Z]{3})[/. ](\d{4})', texto, re.IGNORECASE | re.DOTALL)
    if match_rango:
        try:
            meses = {'ENE': '01', 'FEB': '02', 'MAR': '03', 'ABR': '04', 'MAY': '05', 'JUN': '06', 'JUL': '07', 'AGO': '08', 'SEP': '09', 'OCT': '10', 'NOV': '11', 'DIC': '12'}
            d1, m1_txt, y1 = match_rango.group(1), match_rango.group(2).upper(), match_rango.group(3)
            d2, m2_txt, y2 = match_rango.group(4), match_rango.group(5).upper(), match_rango.group(6)
            datos['periodo'] = f"DEL {d1}/{meses.get(m1_txt, '00')}/{y1} AL {d2}/{meses.get(m2_txt, '00')}/{y2}"
        except: pass
    
    if not datos['periodo']:
        m_per_alt = re.search(r'DEL\s+(\d{2}\s+DE\s+[A-Z]+\s+DE\s+\d{4})\s+AL', texto, re.IGNORECASE)
        if m_per_alt: datos['periodo'] = m_per_alt.group(0).replace('\n', ' ').strip()

    # 3. Cuentas
    m_clabe = re.search(r'(?:CLABE|Cuenta\s+CLABE).*?(\d{18})', texto, re.IGNORECASE | re.DOTALL)
    if m_clabe: datos['numero_cuenta_clabe'] = m_clabe.group(1)
        
    m_cta = re.search(r'Cuenta\s+de\s+Cheques(?:[^0-9]{0,50}?)(\d{10})(?!\d)', texto, re.IGNORECASE | re.DOTALL)
    if m_cta: datos['numero_cuenta'] = m_cta.group(1)
    else:
        m_cta_alt = re.search(r'CONTRATO\s+[:\.]?\s*(\d{10})', texto, re.IGNORECASE)
        if m_cta_alt: datos['numero_cuenta'] = m_cta_alt.group(1)

    # 4. Saldos (Usando funcion_extraer_monto común)
    bloque_resumen = texto[:4000]
    
    def buscar_monto(patron, txt):
        m = re.search(patron, txt, re.IGNORECASE | re.DOTALL)
        return funcion_extraer_monto(m.group(1)) if m else 0.0
        
    datos['saldo_inicial'] = buscar_monto(r'Saldo Anterior\s+([$]?[\d,]+\.\d{2})', bloque_resumen)
    datos['total_depositos'] = buscar_monto(r'Depósitos\s+([$]?[\d,]+\.\d{2})', bloque_resumen)
    datos['total_retiros'] = buscar_monto(r'Retiros\s+([$]?[\d,]+\.\d{2})', bloque_resumen)
    
    for p in PATRONES_SALDO_FINAL:
        sf = buscar_monto(p, texto)
        if sf > 0:
            datos['saldo_final'] = sf
            break
            
    datos['saldo_promedio'] = funcion_extraer_saldo_promedio(texto)

    # Normalización final usando funciones compartidas
    return {
        "Nombre de la empresa del estado de cuenta": funcion_limpiar_nombre_empresa(datos['nombre_empresa']),
        "Numero de cuenta del estado de cuenta": datos['numero_cuenta'],
        "Periodo del estado de cuenta": funcion_formatear_periodo_archivo(datos['periodo']),
        "Saldo inicial de la cuenta": datos['saldo_inicial'],
        "Saldo final de la cuenta": datos['saldo_final'],
        "Saldo promedio del periodo": datos['saldo_promedio'],
        "Cantidad total de depositos": datos['total_depositos'],
        "Cantidad total de retiros": datos['total_retiros'],
        "Giro de la empresa": ""
    }

def funcion_extraer_todas_transacciones(texto, metadatos):
    """
    Extrae transacciones y las normaliza usando el ESTÁNDAR BBVA.
    """
    # 1. Encontrar inicio
    inicio = -1
    for p in [r'DETALLE DE OPERACIONES', r'FECHA\s+CONCEPTO\s+RETIROS', r'FECHA\s+DESCRIPCION']:
        m = re.search(p, texto, re.IGNORECASE)
        if m: 
            inicio = m.end()
            break
    
    if inicio == -1: return []

    # 2. Limpiar texto
    texto_tabla = funcion_limpiar_basura_banamex(texto[inicio:])
    
    # 3. Agrupar por fechas
    grupos = funcion_agrupar_lineas_por_fecha(texto_tabla.split('\n'))
    print(f"✓ Bloques de movimientos encontrados: {len(grupos)}")
    
    # 4. Procesar usando funciones compartidas
    transacciones = []
    contador = {}
    
    anio = '2025'
    if metadatos.get('Periodo del estado de cuenta'):
        m_a = re.search(r'(\d{4})', metadatos['Periodo del estado de cuenta'])
        if m_a: anio = m_a.group(1)
        
    cuenta_propia = metadatos.get('Numero de cuenta del estado de cuenta', '')

    for grupo in grupos:
        tx = funcion_procesar_grupo_transaccion(grupo, anio, contador, cuenta_propia)
        if tx: transacciones.append(tx)
            
    return transacciones

def funcion_limpiar_basura_banamex(texto):
    """Elimina texto repetitivo no deseado."""
    patrones = [
        r'citibanamex', r'Banamex', r'ESTADO DE CUENTA AL.*', r'CLIENTE:\s*\d+',
        r'Página:\s*\d+\s*de\s*\d+', r'DETALLE DE OPERACIONES', r'^\s*\d+\.[A-Z0-9\.]+\s*$'
    ]
    txt = texto
    for p in patrones:
        txt = re.sub(p, '', txt, flags=re.IGNORECASE | re.MULTILINE)
    return txt

def funcion_agrupar_lineas_por_fecha(lineas):
    """Agrupa líneas por fecha (DD MMM)."""
    grupos = []
    grupo_actual = []
    patron = re.compile(r'^\s*(\d{1,2}\s+(?:ENE|FEB|MAR|ABR|MAY|JUN|JUL|AGO|SEP|OCT|NOV|DIC))', re.IGNORECASE)
    
    for l in lineas:
        ls = l.strip()
        if not ls: continue
        if patron.match(ls):
            if grupo_actual: grupos.append(grupo_actual)
            grupo_actual = [ls]
        else:
            if grupo_actual: grupo_actual.append(ls)
    if grupo_actual: grupos.append(grupo_actual)
    return grupos

def funcion_procesar_grupo_transaccion(lineas, anio, contador, cuenta_propia):
    """
    Procesa una transacción usando las MISMAS funciones utilitarias que BBVA
    para garantizar idéntica normalización.
    """
    bloque_texto = " ".join(lineas)
    
    # 1. Fecha (Normalización estándar)
    m_fecha = re.match(r'^(\d{1,2}\s+[A-Z]{3})', lineas[0], re.IGNORECASE)
    if not m_fecha: return None
    
    fecha_raw = m_fecha.group(1)
    fecha_str = f"{fecha_raw}/{anio}".replace(" ", "/")
    fecha = funcion_extraer_fecha_normalizada(fecha_str) # Misma función que BBVA
    
    # 2. Montos
    montos = re.findall(r'([\d,]+\.\d{2})', bloque_texto)
    monto = 0.0
    
    if len(montos) >= 2:
        monto = funcion_extraer_monto(montos[-2])
        texto_analisis = bloque_texto.replace(montos[-1], "").replace(montos[-2], "")
    elif len(montos) == 1:
        monto = funcion_extraer_monto(montos[0])
        texto_analisis = bloque_texto.replace(montos[0], "")
    else:
        return None 
        
    # 3. Clasificación (Usando lógica compartida si es posible, o adaptada)
    es_egreso = _determinar_clasificacion(texto_analisis)
    clasificacion = "Egreso" if es_egreso else "Ingreso"
    
    # 4. Descripción y Nombre Completo (Usando función estándar)
    # Se limpia la fecha del inicio para obtener la descripción base
    desc_base = texto_analisis[len(fecha_raw):].strip()
    nombre_completo = funcion_extraer_nombre_completo_transaccion(lineas, 0, desc_base)
    
    # 5. Beneficiario (Usando función estándar de BBVA)
    # Se intenta extraer con la lógica avanzada compartida
    beneficiario = funcion_extraer_beneficiario_correcto(lineas, "", es_egreso)
    if not beneficiario:
        # Fallback a lógica específica si la estándar no encuentra nada en este formato
        beneficiario = _extraer_beneficiario_banamex_legacy(desc_base)

    # 6. Referencia (Usando función estándar de BBVA + Ajuste Banamex)
    # Se usa la función mejorada, pero se le da contexto específico si es necesario
    referencia = funcion_extraer_referencia_mejorada(lineas)
    
    # Ajuste específico Banamex: Si referencia es vacía o genérica, buscar en otros campos
    if not referencia or referencia == "00000000":
        m_bnet = re.search(r'\b(BNET\w+)\b', nombre_completo)
        if m_bnet: referencia = m_bnet.group(1)
        
    # 7. Cuentas (Usando función estándar de BBVA)
    cuenta_origen, cuenta_destino = funcion_extraer_cuentas_origen_destino(
        lineas, es_egreso, cuenta_propia
    )
    
    # 8. Método de Pago (Usando función estándar de BBVA)
    # Se usa un código ficticio '00' porque Banamex no siempre tiene códigos explícitos
    metodo_pago = funcion_determinar_metodo_pago("00", nombre_completo)
    # Refinamiento para Banamex
    if "CHEQUE" in nombre_completo.upper(): metodo_pago = "Cheque"
    elif "DEPOSITO" in nombre_completo.upper() and "EFECTIVO" in nombre_completo.upper(): metodo_pago = "Efectivo"
    elif "SPEI" in nombre_completo.upper(): metodo_pago = "SPEI"
    elif "DOMI" in nombre_completo.upper(): metodo_pago = "Domiciliación"
    elif metodo_pago == "Otro": metodo_pago = "Transferencia Electrónica"

    # 9. Tipo de Transacción (Normalización)
    if "IVA" in nombre_completo.upper(): tipo_tx = "Impuesto"
    elif "COMISION" in nombre_completo.upper(): tipo_tx = "Comisión"
    elif "INTERES" in nombre_completo.upper(): tipo_tx = "Interés"
    elif "CHEQUE" in nombre_completo.upper(): tipo_tx = "Cheque"
    elif "DEPOSITO" in nombre_completo.upper(): tipo_tx = "Depósito"
    elif "PAGO" in nombre_completo.upper(): tipo_tx = "Pago"
    else: tipo_tx = "Transferencia"

    # 10. Nombre Resumido (Usando función estándar de BBVA)
    nombre_resumido = funcion_crear_nombre_resumido_inteligente(
        nombre_completo, 
        tipo_tx, 
        beneficiario, 
        contador
    )
    
    # Sucursal
    m_suc = re.search(r'SUC\s+(\d{3,4})', nombre_completo)
    sucursal = m_suc.group(1) if m_suc else ""

    return {
        "Fecha de la transacción": fecha,
        "Nombre de la transacción": nombre_completo,
        "Nombre resumido": nombre_resumido,
        "Tipo de transacción": tipo_tx,
        "Clasificación": clasificacion,
        "Quien realiza o recibe el pago": beneficiario,
        "Monto de la transacción": monto,
        "Numero de referencia o folio": referencia,
        "Numero de cuenta origen": cuenta_origen,
        "Numero de cuenta destino": cuenta_destino,
        "Metodo de pago": metodo_pago,
        "Sucursal o ubicacion": sucursal,
        "Giro de la transacción": "",
        "Giro sugerido": "",
        "Análisis monto": "",
        "Análisis contraparte": "",
        "Análisis naturaleza": ""
    }

def _determinar_clasificacion(desc):
    """Clasifica como Ingreso o Egreso."""
    d = desc.upper()
    if any(x in d for x in ['DEPOSITO', 'ABONO', 'PAGO RECIBIDO', 'TRASPASO DE', 'INTERESES GANADOS', 'BONIFICACION', 'DEVOLUCION DE']):
        return False # Ingreso
    if any(x in d for x in ['RETIRO', 'CHEQUE', 'COMISION', 'IVA ', 'PAGO A', 'PAGO INTERBANCARIO', 'TRASPASO A', 'INVERSION', 'COBRO', 'CARGO', 'PAGO DE']):
        return True # Egreso
    return True # Default Egreso

def _extraer_beneficiario_banamex_legacy(desc):
    """Lógica de respaldo para beneficiarios específicos de Banamex."""
    stopwords = ['PAGO', 'RECIBIDO', 'DE', 'A', 'POR', 'ORDEN', 'TRANSFERENCIA', 'SPEI', 'BANCANET', 'REF', 'RASTREO', 'SUC', 'CAJA', 'AUT', 'HORA', 'MISMO', 'DIA']
    palabras = desc.split()
    candidatos = []
    for p in palabras:
        if p.upper() not in stopwords and not re.match(r'^[\d\.:\(\)]+$', p) and len(p) > 2:
            candidatos.append(p)
    return " ".join(candidatos[:6])

def funcion_validar_balance_transacciones(transacciones, metadatos):
    ing = sum(t['Monto de la transacción'] for t in transacciones if t['Clasificación'] == 'Ingreso')
    egr = sum(t['Monto de la transacción'] for t in transacciones if t['Clasificación'] == 'Egreso')
    
    try: dep_meta = metadatos.get('Cantidad total de depositos', 0)
    except: dep_meta = 0.0
        
    try: ret_meta = metadatos.get('Cantidad total de retiros', 0)
    except: ret_meta = 0.0
    
    print(f"Balance Calculado: +{ing:,.2f} | -{egr:,.2f}")
    if abs(ing - dep_meta) < 5.0 and abs(egr - ret_meta) < 5.0:
        print("✓ Balance Validado")
    else:
        print(f"Diferencia en balance (Meta: +{dep_meta:,.2f} | -{ret_meta:,.2f})")