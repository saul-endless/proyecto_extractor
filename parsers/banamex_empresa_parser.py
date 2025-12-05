# -*- coding: utf-8 -*-
"""
Parser Banamex Empresa v9.3 CORREGIDO
- CORRECCIÓN NOMBRE: Filtros anti-leyenda legal.
- CORRECCIÓN CLASIFICACIÓN: Lógica estricta Ingreso vs Egreso.
- CORRECCIÓN SALDO PROMEDIO: Extracción específica + Fallback matemático.
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
    print("\n=== Iniciando Parser Banamex Empresa v9.3 (Fix Nombre/Saldo/Clasif) ===")
    
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
    MEJORA v9.3: Filtros adicionales para ignorar 'Fecha de corte' como nombre.
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
    
    # --- 1. Nombre Empresa (Lógica Mejorada v9.3) ---
    nombre_encontrado = ""
    
    # ESTRATEGIA A: Búsqueda Estructural por Ancla "GAT Real / Inflación"
    idx_ancla = -1
    for i, linea in enumerate(lineas[:80]):
        if "descontar la inflación estimada" in linea.lower() or "gat real es el rendimiento" in linea.lower():
            idx_ancla = i
            break
            
    if idx_ancla != -1:
        for j in range(1, 6): # Buscamos un poco más abajo
            if idx_ancla + j < len(lineas):
                linea_candidata = lineas[idx_ancla + j].strip()
                
                if linea_candidata and len(linea_candidata) > 3 and not re.match(r'^\$?\d', linea_candidata):
                    
                    # Filtros de exclusión MEJORADOS v9.3
                    palabras_prohibidas = [
                        'CALLE', 'AV.', 'AVENIDA', 'COL.', 'C.P.', 'CP ', 'DELEGACION', 'MUNICIPIO', 'SUCURSAL',
                        'INFLACION', 'ESTIMADA', 'DESCONTAR', 'RENDIMIENTO', 'GAT', 'OBTENDRIA', 'IMPUESTOS',
                        # Nuevos filtros específicos para tu error:
                        'FECHA DE CORTE', 'LEYENDA', 'ESTADO DE CUENTA', 'INDICADA'
                    ]
                    
                    if not any(x in linea_candidata.upper() for x in palabras_prohibidas):
                         nombre_encontrado = linea_candidata
                         print(f"  > Nombre detectado por estructura (GAT): {nombre_encontrado}")
                         break

    # ESTRATEGIA B: Búsqueda por Ancla "CLIENTE:"
    if not nombre_encontrado:
        for i, linea in enumerate(lineas[:50]):
            if "CLIENTE:" in linea.upper():
                for k in range(1, 10): 
                    if i + k < len(lineas):
                        l = lineas[i+k].strip()
                        if not l: continue
                        if l.isdigit(): continue
                        if "RFC" in l.upper() or "PÁGINA" in l.upper() or "SUC." in l.upper(): continue
                        if "CUENTA DE CHEQUES" in l.upper() or "MONEDA NACIONAL" in l.upper(): continue
                        if "GAT" in l.upper() or "INTERÉS" in l.upper() or "COMISIONES" in l.upper(): continue
                        if "INFLACION" in l.upper() or "ESTIMADA" in l.upper(): continue
                        # Filtro extra v9.3
                        if "FECHA DE CORTE" in l.upper(): continue

                        if len(l) > 5 and not any(x in l.upper() for x in ['CALLE', 'AVENIDA', 'COL.']):
                             if re.search(r'\b(SA DE CV|S\.A\.|S\.C\.|SOCIEDAD|ASOCIACION|GRUPO|CORPORATIVO|INMOVITUR|SC DE RL)\b', l, re.IGNORECASE):
                                 nombre_encontrado = l
                                 print(f"  > Nombre detectado por estructura (Cliente): {nombre_encontrado}")
                                 break
                if nombre_encontrado: break

    # ESTRATEGIA C: Regex (Fallback original)
    if not nombre_encontrado:
        candidatos_nombre = []
        for linea in lineas[:60]:
            l = linea.strip()
            if len(l) < 5: continue
            
            filtros = ['BANAMEX', 'SUCURSAL', 'RFC', 'CLIENTE', 'PÁGINA', 'ESTADO DE CUENTA', 'ACTUARIO', 
                       'SANTA FE', 'COL.', 'C.P.', 'CIUDAD DE MEXICO', 'CALLE', 'AVENIDA', 'TORRE',
                       'INFLACION', 'ESTIMADA', 'DESCONTAR', 'RENDIMIENTO', 
                       'FECHA DE CORTE', 'LEYENDA'] # Filtro agregado
            
            if any(x in l.upper() for x in filtros): 
                continue
            
            if re.search(r'\b(SA DE CV|S\.A\.|S\.C\.|SOCIEDAD|ASOCIACION|GRUPO|CORPORATIVO|INMOVITUR|SC DE RL|S\.A\.B\.)\b', l, re.IGNORECASE):
                nombre_encontrado = l
                break
            
            if l.isupper() and not re.search(r'\d', l):
                candidatos_nombre.append(l)
                
        if not nombre_encontrado and candidatos_nombre:
            nombre_encontrado = candidatos_nombre[0]

    datos['nombre_empresa'] = nombre_encontrado if nombre_encontrado else "EMPRESA NO IDENTIFICADA"

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
    
    # --- CORRECCIÓN SALDO PROMEDIO (Regex + Fallback) v9.3 ---
    # Intentar extraer explícitamente primero
    match_prom = re.search(r'Saldo Promedio.*?([$]?[\d,]+\.\d{2})', texto, re.IGNORECASE | re.DOTALL)
    if match_prom:
        datos['saldo_promedio'] = funcion_extraer_monto(match_prom.group(1))
    
    # Si falla la extracción (es 0), calcular matemáticamente
    if datos['saldo_promedio'] == 0.0 and datos['saldo_inicial'] > 0:
        datos['saldo_promedio'] = (datos['saldo_inicial'] + datos['saldo_final']) / 2
        print(f"  > Saldo Promedio calculado matemáticamente: {datos['saldo_promedio']}")

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
        
    # 3. Clasificación (Mejorada v9.3)
    es_egreso = _determinar_clasificacion(texto_analisis)
    clasificacion = "Egreso" if es_egreso else "Ingreso"
    
    # 4. Descripción y Nombre Completo
    desc_base = texto_analisis[len(fecha_raw):].strip()
    nombre_completo = funcion_extraer_nombre_completo_transaccion(lineas, 0, desc_base)
    
    # 5. Beneficiario
    beneficiario = funcion_extraer_beneficiario_correcto(lineas, "", es_egreso)
    if not beneficiario:
        beneficiario = _extraer_beneficiario_banamex_legacy(desc_base)

    # 6. Referencia
    referencia = funcion_extraer_referencia_mejorada(lineas)
    if not referencia or referencia == "00000000":
        m_bnet = re.search(r'\b(BNET\w+)\b', nombre_completo)
        if m_bnet: referencia = m_bnet.group(1)
        
    # 7. Cuentas
    cuenta_origen, cuenta_destino = funcion_extraer_cuentas_origen_destino(
        lineas, es_egreso, cuenta_propia
    )
    
    # 8. Método de Pago
    metodo_pago = funcion_determinar_metodo_pago("00", nombre_completo)
    if "CHEQUE" in nombre_completo.upper(): metodo_pago = "Cheque"
    elif "DEPOSITO" in nombre_completo.upper() and "EFECTIVO" in nombre_completo.upper(): metodo_pago = "Efectivo"
    elif "SPEI" in nombre_completo.upper(): metodo_pago = "SPEI"
    elif "DOMI" in nombre_completo.upper(): metodo_pago = "Domiciliación"
    elif metodo_pago == "Otro": metodo_pago = "Transferencia Electrónica"

    # 9. Tipo de Transacción
    if "IVA" in nombre_completo.upper(): tipo_tx = "Impuesto"
    elif "COMISION" in nombre_completo.upper(): tipo_tx = "Comisión"
    elif "INTERES" in nombre_completo.upper(): tipo_tx = "Interés"
    elif "CHEQUE" in nombre_completo.upper(): tipo_tx = "Cheque"
    elif "DEPOSITO" in nombre_completo.upper(): tipo_tx = "Depósito"
    elif "PAGO" in nombre_completo.upper(): tipo_tx = "Pago"
    else: tipo_tx = "Transferencia"

    # 10. Nombre Resumido
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
    """
    Clasifica como Ingreso o Egreso.
    MEJORA v9.3: Orden estricto de prioridades.
    """
    d = desc.upper()
    
    # 1. Ingresos explícitos (Prioridad Alta)
    if any(x in d for x in ['PAGO RECIBIDO', 'DEPOSITO', 'ABONO', 'DEVUELTO', 'BONIFICACION', 'INTERESES GANADOS']):
        return False # Es Ingreso
        
    # 2. Egresos explícitos
    if any(x in d for x in ['PAGO A', 'PAGO INTERBANCARIO', 'CHEQUE', 'COMISION', 'RETIRO', 'CARGO', 'TRASPASO ENTRE', 'INVERSION']):
        return True # Es Egreso
        
    # 3. Default
    return True # Egreso por seguridad

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