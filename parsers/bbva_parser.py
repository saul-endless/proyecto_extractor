# Se importan las bibliotecas necesarias
import re
from decimal import Decimal
from utils.validators import limpiar_monto

# Se definen los patrones regex para BBVA
PATRON_NOMBRE_EMPRESA = re.compile(
    r"^([A-ZÁÉÍÓÚÑ\s]+(?:SA|S\.A\.|DE CV|S\. DE R\.L\.|SC|S\.C\.)[^\n]*)",
    re.MULTILINE | re.IGNORECASE
)

PATRON_PERIODO = re.compile(
    r"Periodo\s+(?:DEL\s+)?(\d{2}/\d{2}/\d{4})\s+AL\s+(\d{2}/\d{2}/\d{4})",
    re.IGNORECASE
)

PATRON_CLABE = re.compile(
    r"(?:No\.\s*)?Cuenta\s*CLABE\s+(\d{18})",
    re.IGNORECASE
)

PATRON_SALDO_INICIAL = re.compile(
    r"Saldo de (?:Operación|Liquidación) Inicial\s+([\d,.-]+)",
    re.IGNORECASE
)

PATRON_DEPOSITOS = re.compile(
    r"Depósitos\s*/?\\?\s*Abonos\s*\(\+\)\s+(\d+)\s+([\d,.-]+)",
    re.IGNORECASE
)

PATRON_RETIROS = re.compile(
    r"Retiros\s*/?\\?\s*Cargos\s*\(\-\)\s+(\d+)\s+([\d,.-]+)",
    re.IGNORECASE
)

PATRON_SALDO_FINAL = re.compile(
    r"Saldo\s+(?:Final|de\s+Operación\s+Final)\s*\(\+\)\s+([\d,.-]+)",
    re.IGNORECASE
)

PATRON_SALDO_PROMEDIO = re.compile(
    r"Saldo\s+Promedio(?:\s+Mínimo\s+Mensual\s+Hasta)?:?\s+([\d,.-]+)",
    re.IGNORECASE
)

PATRON_INICIO_MOVIMIENTOS = re.compile(
    r"Detalle de Movimientos Realizados",
    re.IGNORECASE
)

def _encontrar_pagina_info_financiera(paginas):
    """
    Se busca la pagina que contiene Informacion Financiera.
    """
    marcadores_requeridos = [
        "Información Financiera",
        "MONEDA NACIONAL",
        "Comportamiento"
    ]
    
    for i, pagina in enumerate(paginas):
        if all(marcador in pagina for marcador in marcadores_requeridos):
            return pagina
    
    for i, pagina in enumerate(paginas):
        coincidencias = sum(1 for m in marcadores_requeridos if m in pagina)
        if coincidencias >= 2:
            return pagina
    
    return None

def _extraer_nombre_empresa_robusto(texto):
    """
    Se extrae el nombre de la empresa con multiples estrategias.
    """
    patron = re.search(
        r"^([A-ZÁÉÍÓÚÑ\s]+(?:SA|S\.A\.|DE CV|S\. DE R\.L\.|SC|S\.C\.)[^\n]*)",
        texto,
        re.MULTILINE | re.IGNORECASE
    )
    if patron:
        nombre = patron.group(1).strip()
        if len(nombre) > 5 and 'BBVA' not in nombre.upper():
            return nombre
    
    lineas = texto.split('\n')[:20]
    for linea in lineas:
        if any(palabra in linea.upper() for palabra in ['SA DE CV', 'S.A. DE C.V.', 'S. DE R.L.']):
            if 'BBVA' not in linea.upper() and len(linea.strip()) > 10:
                return linea.strip()
    
    return None

def parsear_datos_generales(paginas_texto):
    """
    Se extraen los 8 campos de Datos Generales del estado de cuenta BBVA.
    Se recibe lista de paginas como parametro.
    """
    datos = {
        'nombre_empresa': None,
        'numero_cuenta_clabe': None,
        'periodo': None,
        'saldo_inicial': Decimal('0.00'),
        'saldo_final': Decimal('0.00'),
        'saldo_promedio': Decimal('0.00'),
        'total_depositos': Decimal('0.00'),
        'total_retiros': Decimal('0.00')
    }
    
    pagina_info = _encontrar_pagina_info_financiera(paginas_texto)
    
    if not pagina_info:
        print("  > Advertencia: No se encontro la pagina de Informacion Financiera")
        pagina_info = '\n'.join(paginas_texto)
    
    datos['nombre_empresa'] = _extraer_nombre_empresa_robusto(pagina_info)
    if not datos['nombre_empresa']:
        for pagina in paginas_texto:
            nombre = _extraer_nombre_empresa_robusto(pagina)
            if nombre:
                datos['nombre_empresa'] = nombre
                break
    
    match_periodo = PATRON_PERIODO.search(pagina_info)
    if match_periodo:
        fecha_inicio = match_periodo.group(1)
        fecha_fin = match_periodo.group(2)
        datos['periodo'] = f"DEL {fecha_inicio} AL {fecha_fin}"
    
    match_clabe = PATRON_CLABE.search(pagina_info)
    if match_clabe:
        datos['numero_cuenta_clabe'] = match_clabe.group(1)
    
    match_saldo_ini = PATRON_SALDO_INICIAL.search(pagina_info)
    if match_saldo_ini:
        datos['saldo_inicial'] = limpiar_monto(match_saldo_ini.group(1))
    
    match_depositos = PATRON_DEPOSITOS.search(pagina_info)
    if match_depositos:
        datos['total_depositos'] = limpiar_monto(match_depositos.group(2))
    
    match_retiros = PATRON_RETIROS.search(pagina_info)
    if match_retiros:
        datos['total_retiros'] = limpiar_monto(match_retiros.group(2))
    
    match_saldo_fin = PATRON_SALDO_FINAL.search(pagina_info)
    if match_saldo_fin:
        datos['saldo_final'] = limpiar_monto(match_saldo_fin.group(1))
    
    match_saldo_prom = PATRON_SALDO_PROMEDIO.search(pagina_info)
    if match_saldo_prom:
        datos['saldo_promedio'] = limpiar_monto(match_saldo_prom.group(1))
    
    return datos

def parsear_transacciones(paginas_texto, saldo_inicial):
    """
    Se extraen todas las transacciones del estado de cuenta BBVA.
    Se recibe lista de paginas como parametro.
    """
    texto_movimientos = ""
    encontrado_inicio = False
    
    for pagina in paginas_texto:
        if PATRON_INICIO_MOVIMIENTOS.search(pagina):
            match = PATRON_INICIO_MOVIMIENTOS.search(pagina)
            texto_movimientos += pagina[match.end():]
            encontrado_inicio = True
        elif encontrado_inicio:
            texto_movimientos += '\n' + pagina
            
            if 'Total de Movimientos' in pagina:
                idx = texto_movimientos.find('Total de Movimientos')
                if idx != -1:
                    texto_movimientos = texto_movimientos[:idx]
                break
    
    if not encontrado_inicio:
        print("  > Advertencia: No se encontro el 'Detalle de Movimientos Realizados' en BBVA.")
        return []
    
    transacciones = _extraer_transacciones_multilinea(texto_movimientos, saldo_inicial)
    
    print(f"  > Se extrajeron {len(transacciones)} transacciones")
    
    return transacciones

def _extraer_transacciones_multilinea(texto_movimientos, saldo_inicial):
    """
    Se extraen transacciones considerando formato multi-linea.
    """
    transacciones = []
    lineas = texto_movimientos.split('\n')
    
    transaccion_actual = None
    lineas_descripcion = []
    
    for linea in lineas:
        match_inicio = re.match(
            r'^(\d{2}/[A-Z]{3})\s+(\d{2}/[A-Z]{3})\s+([A-Z]\d{1,2})\s+(.+)',
            linea
        )
        
        if match_inicio:
            if transaccion_actual:
                _procesar_transaccion_completa(
                    transaccion_actual, 
                    lineas_descripcion, 
                    transacciones
                )
            
            transaccion_actual = {
                'fecha_operacion': match_inicio.group(1),
                'fecha_liquidacion': match_inicio.group(2),
                'codigo': match_inicio.group(3),
                'descripcion_linea1': match_inicio.group(4).strip()
            }
            lineas_descripcion = []
        
        elif transaccion_actual and linea.strip():
            lineas_descripcion.append(linea.strip())
    
    if transaccion_actual:
        _procesar_transaccion_completa(
            transaccion_actual, 
            lineas_descripcion, 
            transacciones
        )
    
    return transacciones

def _procesar_transaccion_completa(trans, lineas_extra, lista_trans):
    """
    Se procesa una transaccion completa extrayendo todos sus campos.
    """
    descripcion_completa = trans['descripcion_linea1'] + ' ' + ' '.join(lineas_extra)
    
    cargo = None
    abono = None
    
    for linea in lineas_extra:
        match_montos = re.search(r'([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})$', linea)
        if match_montos:
            cargo_o_abono = limpiar_monto(match_montos.group(1))
            break
    
    match_final = re.search(r'([\d,]+\.\d{2})\s+([\d,]+\.\d{2})$', descripcion_completa)
    if match_final:
        descripcion_completa = descripcion_completa[:match_final.start()].strip()
    
    if cargo_o_abono and cargo_o_abono > 0:
        if trans['codigo'] in ['T17', 'N06', 'A15', 'G30', 'S39', 'S40', 'CI', 'COM']:
            monto = cargo_o_abono
            clasificacion = "Egreso"
        else:
            monto = cargo_o_abono
            clasificacion = "Ingreso"
    else:
        monto = Decimal('0.00')
        clasificacion = "Desconocido"
    
    referencia = _extraer_referencia(descripcion_completa)
    cuenta_origen_destino = _extraer_cuenta(descripcion_completa)
    nombre_contraparte = _extraer_nombre_contraparte(descripcion_completa, trans['codigo'])
    
    tipo_transaccion = _determinar_tipo_transaccion(trans['codigo'])
    metodo_pago = _determinar_metodo_pago(trans['codigo'], descripcion_completa)
    
    transaccion = {
        "fecha": trans['fecha_operacion'],
        "nombre_transaccion": re.sub(r'\s+', ' ', descripcion_completa).strip(),
        "nombre_resumido": "", 
        "tipo_transaccion": tipo_transaccion, 
        "clasificacion": clasificacion,
        "quien_realiza_o_recibe": nombre_contraparte, 
        "monto": monto,
        "numero_referencia_folio": referencia, 
        "numero_cuenta_origen_destino": cuenta_origen_destino, 
        "metodo_pago": metodo_pago, 
        "sucursal_o_ubicacion": "" 
    }
    
    lista_trans.append(transaccion)

def _extraer_referencia(descripcion):
    """
    Se extrae numero de referencia de la descripcion.
    """
    match = re.search(r'Ref\.?\s*:?\s*(\w+)', descripcion, re.IGNORECASE)
    if match:
        return match.group(1)
    
    match = re.search(r'\b(\d{7,})\b', descripcion)
    if match:
        return match.group(1)
    
    return ""

def _extraer_cuenta(descripcion):
    """
    Se extrae numero de cuenta de origen o destino.
    """
    match = re.search(r'\b(\d{18})\b', descripcion)
    if match:
        return match.group(1)
    
    match = re.search(r'(?:CTA\.?|CUENTA)\s*(?:ORDENANTE|DESTINO)?\s*:?\s*(\d{10,16})', descripcion, re.IGNORECASE)
    if match:
        return match.group(1)
    
    return ""

def _extraer_nombre_contraparte(descripcion, codigo):
    """
    Se extrae el nombre de quien realiza o recibe el pago.
    """
    if codigo in ['T17', 'T20']:
        match = re.search(r'(?:PAGO\s+(?:RECIBIDO\s+)?(?:DE|POR\s+ORDEN\s+DE)|ENVIADO\s+A?)\s+([A-Z\s]+?)(?:\s+CTA|$)', descripcion, re.IGNORECASE)
        if match:
            nombre = match.group(1).strip()
            nombre = re.sub(r'^(?:BBVA|SANTANDER|BANORTE|HSBC|INBURSA|BANAMEX|AZTECA|STP|BANREGIO)\s*', '', nombre, flags=re.IGNORECASE)
            return nombre.strip()
    
    if codigo == 'W02':
        match = re.search(r'^(.*?)(?:\s+BMRCASH|\s+Ref\.)', descripcion, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    
    if codigo == 'N06':
        match = re.search(r'BNET\s+\d+\s+([A-Z\s]+?)(?:\s+Ref\.)', descripcion, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    
    if codigo in ['A15', 'G30', 'S39', 'S40']:
        match = re.search(r'^([A-Z\s\*#]+?)(?:\s+RFC:|$)', descripcion, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    
    return ""

def _determinar_tipo_transaccion(codigo):
    """
    Se determina el tipo de transaccion basado en el codigo.
    """
    tipos = {
        'T17': 'Transferencia',
        'T20': 'Transferencia',
        'W02': 'Deposito',
        'N06': 'Pago de Tercero',
        'A15': 'Cargo por Servicio',
        'G30': 'Recibo',
        'S39': 'Comision',
        'S40': 'IVA',
        'CI': 'Cobro Inmediato',
        'COM': 'Comision'
    }
    return tipos.get(codigo, 'Otro')

def _determinar_metodo_pago(codigo, descripcion):
    """
    Se determina el metodo de pago.
    """
    if 'SPEI' in descripcion.upper():
        return 'SPEI'
    elif codigo in ['T17', 'T20']:
        return 'SPEI'
    elif codigo == 'W02':
        return 'Deposito en Efectivo' if 'CASH' in descripcion.upper() else 'Deposito'
    elif codigo in ['A15']:
        return 'Tarjeta de Credito'
    elif codigo == 'N06':
        return 'Banca en Linea'
    else:
        return 'Otro'