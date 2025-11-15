# -*- coding: utf-8 -*-
"""
Funciones auxiliares para extracción de campos - Versión MEJORADA v5.5
Todas las funciones en español con prefijo funcion_
- Lógica de beneficiario v5.3 (prioriza códigos de comisión)
- Lógica de fecha mejorada (detecta año del nombre de archivo)
- Lógica de códigos v5.5 (Se elimina N06 de cargos)
"""

import re
from datetime import datetime
import sys
import os # Asegurarse que os esté importado


def funcion_extraer_fecha_normalizada(fecha_texto):
    """
    Se convierte fecha del formato DD/MMM al formato DD/MM/AAAA.
    Se asume el año correcto basado en el nombre del PDF.
    """
    meses = {
        'ENE': '01', 'FEB': '02', 'MAR': '03', 'ABR': '04',
        'MAY': '05', 'JUN': '06', 'JUL': '07', 'AGO': '08',
        'SEP': '09', 'OCT': '10', 'NOV': '11', 'DIC': '12'
    }
    
    año_detectado = '2025' # Default
    
    # Se busca el nombre del archivo en los argumentos del script
    nombre_archivo_procesado = ""
    for arg in sys.argv:
        if '.pdf' in arg.lower():
            # Se extrae el nombre base del archivo
            nombre_archivo_procesado = os.path.basename(arg).lower()
            break
            
    if '2024' in nombre_archivo_procesado:
        año_detectado = '2024'
    elif '2025' in nombre_archivo_procesado:
        año_detectado = '2025'
    
    match = re.match(r'(\d{2})/([A-Z]{3})', fecha_texto)
    if match:
        dia = match.group(1)
        mes_texto = match.group(2)
        mes = meses.get(mes_texto, '01')
        return f"{dia}/{mes}/{año_detectado}"
    
    # Si ya está en formato DD/MM/AAAA
    if re.match(r'\d{2}/\d{2}/\d{4}', fecha_texto):
        return fecha_texto
    
    return f"01/01/{año_detectado}"  # Fecha por defecto


def funcion_extraer_monto(texto_monto):
    """
    Se extrae el monto numérico de un texto.
    Se eliminan comas y se convierte a float.
    """
    if not texto_monto:
        return 0.0
    
    texto_limpio = str(texto_monto).replace(',', '').replace('$', '').replace('-', '').strip()
    
    match = re.search(r'(\d+(?:\.\d{2})?)', texto_limpio)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return 0.0
    
    return 0.0


def _es_linea_beneficiario(linea):
    """
    Se determina si una línea es un nombre de beneficiario.
    (Ej. "JEAN EMMANUEL ABONCE LEAL" o "Enrique Color")
    """
    linea_limpia = linea.strip()
    if not linea_limpia:
        return False
    
    es_mayusculas = bool(re.match(r'^[A-Z\s.]+$', linea_limpia))
    tiene_palabras = len(linea_limpia.split()) >= 2
    es_largo_minimo = len(linea_limpia) > 5
    no_es_keyword = not any(kw in linea_limpia for kw in [
        'BBVA', 'BNET', 'REF', 'SPEI', 'RFC', 'AUT', 'CUENTA', 'PAGO',
        'ESTADO DE CUENTA', 'INFORMACION', 'TECNOLOGIAS', 'INNOVATION',
        'SA DE CV', 'BMRCASH', 'PRESTAMO', 'FECHA', 'SALDO', 'OPER', 'LIQ',
        'COD. DESCRIPCION', 'REFERENCIA', 'CARGOS', 'ABONOS'
    ])
    
    return es_mayusculas and es_largo_minimo and no_es_keyword and tiene_palabras


def funcion_extraer_nombre_completo_transaccion(lineas_grupo, indice_linea_principal, descripcion_raw):
    """
    Se extrae el nombre completo de todas las líneas del grupo.
    Se concatenan con espacios, pero se excluyen líneas de beneficiario.
    """
    if not lineas_grupo:
        return ""
    
    partes_nombre = [descripcion_raw]

    # Se procesan las líneas *después* de la principal
    for i in range(indice_linea_principal + 1, len(lineas_grupo)):
        linea_limpia = lineas_grupo[i].strip()
        
        # Se omite si es una línea de beneficiario
        if _es_linea_beneficiario(linea_limpia):
            continue
            
        # Se omite si es un encabezado o pie
        if any(palabra in linea_limpia.upper() for palabra in [
            'ESTADO DE CUENTA', 'PAGINA', 'BBVA', 'INFORMACION',
            'TOTAL DE MOVIMIENTOS', 'MAESTRA PYME', 'FECHA', 'SALDO'
        ]):
            break
                        
        # Se agrega si tiene contenido válido
        if linea_limpia and len(linea_limpia) > 2:
            partes_nombre.append(linea_limpia)
    
    return ' '.join(partes_nombre)


def funcion_extraer_beneficiario_correcto(lineas_grupo, codigo, es_cargo):
    """
    Se extrae el beneficiario/ordenante CORRECTO de la transacción.
    v5.3: Se priorizan códigos de comisión/impuestos.
    """
    texto_completo_upper = ' '.join(lineas_grupo).upper()

    # 1. Se priorizan códigos bancarios
    if codigo in ['S39', 'S40', 'G30', 'A16', 'A17']:
        return "BBVA"
    if codigo == 'P14' or 'SAT' in texto_completo_upper:
        return "SAT"

    # 2. Se busca una línea que sea *solo* un nombre (ej. "Enrique Color")
    for linea in lineas_grupo:
        if _es_linea_beneficiario(linea):
            return linea.strip()

    # 3. Si no se encuentra, se buscan patrones dentro de la descripción
    # Para SPEI Enviado
    if codigo == 'T17' or 'SPEI ENVIADO' in texto_completo_upper:
        # Se busca el nombre después del banco
        for banco in ['INBURSA', 'BANORTE', 'HSBC', 'SANTANDER', 'AZTECA', 'BANREGIO', 'STP', 'BANAMEX', 'SCOTIABANK', 'AFIRME', 'BANCOPPEL', 'NU MEXICO', 'MERCADO PAGO']:
            patron = rf'{banco}\s+([A-Z][A-Z\s]+?)(?:\s+\d{{2}}|\s+Ref\.|\s+BNET|\s+\d{{8}})'
            match = re.search(patron, texto_completo_upper)
            if match:
                nombre = match.group(1).strip()
                if len(nombre) > 5 and not nombre.isdigit():
                    return nombre
    
    # Para pagos con tarjeta (A15)
    if codigo == 'A15':
        # Se extrae el nombre del comercio (ej. GOOGLE, VIVA AEROBUS, LIVERPOOL)
        match_comercio = re.search(r'A15\s+([A-Z0-9*#\s_]+?)(?:\s+RFC:|\s+USD|\s+\d{2}:\d{2})', ' '.join(lineas_grupo))
        if match_comercio:
            comercio = match_comercio.group(1).strip().replace('*', ' ').replace('#', ' ')
            comercio = re.sub(r'\s+', ' ', comercio) # Se limpian espacios extra
            return comercio.upper()

    return ""


def funcion_extraer_referencia_mejorada(lineas_grupo):
    """
    Se extrae SOLO la referencia numérica o alfanumérica.
    NO se extraen nombres de empresas.
    """
    texto_completo = ' '.join(lineas_grupo)
    
    # 1. Se busca el patrón "Ref. XXXXX"
    match_ref = re.search(r'Ref\.\s+([A-Z0-9*#]+)\b', texto_completo, re.IGNORECASE)
    if match_ref:
        referencia = match_ref.group(1)
        if '******' not in referencia: # Se ignora la ref de tarjeta
            return referencia

    # 2. Se busca el patrón "AUT XXXXX" (Autorización)
    match_aut = re.search(r'AUT[:\s]+(\d{6,})', texto_completo, re.IGNORECASE)
    if match_aut:
        return match_aut.group(1)
        
    # 3. Se buscan códigos alfanuméricos largos (BNET, REFBNTC)
    for linea in lineas_grupo:
        # (ej. BNET01002410020040771417)
        match_bnet = re.search(r'\b(BNET[A-Z0-9]{10,})\b', linea)
        if match_bnet:
            return match_bnet.group(1)
        # (ej. REFBNTC00335630)
        match_refbntc = re.search(r'\b(REFBNTC[A-Z0-9]{8,})\b', linea)
        if match_refbntc:
            return match_refbntc.group(1)
            
    # 4. Se busca un número largo que esté solo en una línea (probable folio)
    for linea in lineas_grupo[1:]: 
        if _es_linea_beneficiario(linea):
            continue
        match_num = re.search(r'^\s*(\d{8,15})\s*$', linea)
        if match_num:
            return match_num.group(1)
            
    # 5. Fallback: Se busca un número de referencia en la descripción
    match_ref_desc = re.search(r'Ref\.\s+(\d+)\b', texto_completo)
    if match_ref_desc:
        return match_ref_desc.group(1)

    return ""


def funcion_crear_nombre_resumido_inteligente(nombre_completo, tipo_transaccion, beneficiario, contador_transacciones):
    """
    Se crea un nombre resumido descriptivo e inteligente.
    Se usa contador para transacciones repetidas.
    """
    beneficiario_limpio = beneficiario if beneficiario else ""
    
    nombre_corto = beneficiario_limpio
    if len(nombre_corto) > 25:
        palabras = nombre_corto.split()
        nombre_corto = ' '.join(palabras[:3])
        
    clave_transaccion = f"{tipo_transaccion}_{nombre_corto}"
    
    # Incrementar contador
    if clave_transaccion not in contador_transacciones:
        contador_transacciones[clave_transaccion] = 0
    contador_transacciones[clave_transaccion] += 1
    numero = contador_transacciones[clave_transaccion]
    
    # Se genera el nombre basado en las categorías solicitadas
    if tipo_transaccion == 'Transferencia':
        if 'SPEI ENVIADO' in nombre_completo.upper():
            return f"Transferencia SPEI a {nombre_corto}" if beneficiario_limpio else "Transferencia SPEI a tercero"
        elif 'SPEI RECIBIDO' in nombre_completo.upper():
            return f"Transferencia SPEI de {nombre_corto}" if beneficiario_limpio else "Transferencia SPEI de tercero"
        elif 'SPEI DEVUELTO' in nombre_completo.upper():
            return f"Devolución SPEI de {nombre_corto}" if beneficiario_limpio else "Devolución SPEI"
        else:
            return f"Transferencia a {nombre_corto}" if beneficiario_limpio else f"Transferencia de {nombre_corto}"
    
    elif tipo_transaccion == 'Depósito':
        return f"Depósito de {nombre_corto}" if beneficiario_limpio else f"Depósito de tercero ({numero}/n)"

    elif tipo_transaccion == 'Tarjeta':
        if 'GOOGLE' in nombre_completo.upper():
            return "Suscripción mensual GOOGLE GSUITE"
        elif 'GODADDY' in nombre_completo.upper():
            return f"Compra en línea GODADDY ({numero}/n)"
        elif 'MICROSOFT' in nombre_completo.upper():
            return "Compra en línea MICROSOFT"
        elif 'WIXCOM' in nombre_completo.upper():
            return f"Suscripción mensual WIXCOM ({numero}/n)"
        elif 'ADOBE' in nombre_completo.upper():
            return "Suscripción mensual ADOBE"
        elif beneficiario_limpio:
            return f"Compra en {beneficiario_limpio}"
        else:
            return "Pago con tarjeta"

    elif tipo_transaccion == 'Comisión':
        if 'IVA' in nombre_completo.upper():
            return "IVA de comisión servicio banca por internet"
        else:
            return "Comisión por servicio de banca por internet"

    elif tipo_transaccion == 'Impuesto':
        return "Pago de ISR"
        
    elif tipo_transaccion == 'Retiro':
        return "Retiro cajero automático"

    elif tipo_transaccion == 'Pago':
        return f"Pago cuenta de tercero {nombre_corto}" if beneficiario_limpio else "Pago cuenta de tercero"

    elif tipo_transaccion == 'Cargo':
        return f"Cargo por recibo ({numero}/n)"

    else:
        return f"{tipo_transaccion} {nombre_corto}"[:50] if beneficiario_limpio else tipo_transaccion


def funcion_extraer_saldo_promedio(texto_completo):
    """
    Se extrae el saldo promedio del periodo.
    Se busca en la sección de Rendimiento.
    """
    match_rendimiento = re.search(r'Rendimiento', texto_completo, re.IGNORECASE)
    if not match_rendimiento:
        return 0.0
    
    inicio = match_rendimiento.start()
    seccion = texto_completo[inicio:inicio + 1000]
    
    patron = r'Saldo\s+Promedio\s+([\d,]+\.?\d*)'
    match = re.search(patron, seccion, re.IGNORECASE)
    
    if match:
        return funcion_extraer_monto(match.group(1))
    
    return 0.0


def funcion_limpiar_nombre_empresa(nombre_empresa):
    """
    Se limpia el nombre de la empresa.
    Se reemplazan caracteres especiales por guión.
    """
    if not nombre_empresa:
        return ""
    
    caracteres_invalidos = ['/', '\\', ':', '?', '"', '<', '>', '|']
    nombre_limpio = nombre_empresa
    for caracter in caracteres_invalidos:
        nombre_limpio = nombre_limpio.replace(caracter, '-')
    
    nombre_limpio = re.sub(r'-+', '-', nombre_limpio)
    nombre_limpio = re.sub(r'\s+', ' ', nombre_limpio)
    
    return nombre_limpio.strip()


def funcion_formatear_periodo_archivo(periodo_texto):
    """
    Se formatea el periodo para nombre de archivo.
    Formato: DDMMMAAAA_DDMMMAAAA
    """
    if not periodo_texto:
        return "PERIODO_NO_DEFINIDO"
    
    patron = r'(\d{2})/(\d{2})/(\d{4})'
    matches = re.findall(patron, periodo_texto)
    
    if len(matches) >= 2:
        fecha1 = matches[0]
        fecha2 = matches[1]
        
        meses = {
            '01': 'ENE', '02': 'FEB', '03': 'MAR', '04': 'ABR',
            '05': 'MAY', '06': 'JUN', '07': 'JUL', '08': 'AGO',
            '09': 'SEP', '10': 'OCT', '11': 'NOV', '12': 'DIC'
        }
        
        dia1, mes1, año1 = fecha1
        dia2, mes2, año2 = fecha2
        
        mes1_texto = meses.get(mes1, 'MES')
        mes2_texto = meses.get(mes2, 'MES')
        
        return f"{dia1}{mes1_texto}{año1}_{dia2}{mes2_texto}{año2}"
    
    return "PERIODO_NO_DEFINIDO"


def funcion_determinar_metodo_pago(codigo, descripcion):
    """
    Se determina el método de pago de la transacción.
    Se basa en código y descripción.
    """
    descripcion_upper = descripcion.upper()
    
    if codigo in ['T17', 'T20', 'T22'] or 'SPEI' in descripcion_upper:
        return 'SPEI'
    elif codigo == 'N06':
        return 'Transferencia'
    elif codigo == 'W02':
        return 'Efectivo'
    elif codigo in ['A15', 'A16', 'A17'] or 'TARJETA' in descripcion_upper:
        return 'Tarjeta'
    elif codigo == 'A01':
        return 'Retiro Cajero'
    elif codigo in ['S39', 'S40']:
        return 'Cargo bancario'
    elif codigo == 'P14':
        return 'Pago de impuestos'
    elif 'CHEQUE' in descripcion_upper:
        return 'Cheque'
    else:
        return 'Otro'


def funcion_extraer_cuentas_origen_destino(lineas_grupo, es_cargo, cuenta_propia):
    """
    Se separan los números de cuenta origen y destino.
    Se devuelven dos valores separados.
    """
    texto_completo = ' '.join(lineas_grupo)
    
    # Se buscan todas las cuentas/clabes en el texto
    cuentas = re.findall(r'\b(\d{10,18})\b', texto_completo)
    
    cuenta_tercero = ""
    for cuenta in cuentas:
        # Se filtran cuentas válidas (longitud)
        if len(cuenta) in [10, 11, 16, 18]:
            # Se asegura que no sea la cuenta propia (si ya la conocemos)
            if cuenta != cuenta_propia:
                # Se excluyen folios que parecen cuentas (ej. años)
                if not (cuenta.startswith('2024') or cuenta.startswith('2025')):
                    cuenta_tercero = cuenta
                    break # Se toma la primera cuenta de tercero encontrada
    
    if es_cargo:
        # Para cargos, la cuenta propia es origen, la encontrada es destino
        cuenta_origen = cuenta_propia
        cuenta_destino = cuenta_tercero
    else:
        # Para abonos, la cuenta propia es destino, la encontrada es origen
        cuenta_origen = cuenta_tercero
        cuenta_destino = cuenta_propia
    
    return cuenta_origen, cuenta_destino


def funcion_es_codigo_cargo(codigo):
    """
    Se determina si un código corresponde a un cargo.
    v5.6: Se revierte el cambio de N06. La lógica ahora está en el parser.
    """
    # Códigos de Egreso (Cargos)
    codigos_cargo = {
        'T17', # SPEI Enviado
        'A15', # Compra Tarjeta
        'A16', # Reposición Tarjeta
        'A17', # IVA Reposición Tarjeta
        'G30', # Recibo
        'S39', # Comisión Serv Banca Internet
        'S40', # IVA Comisión
        'P14', # Pago SAT
        'N06', # N06 VUELVE A SER EGRESO POR DEFECTO
        'A01', # Retiro Cajero
        'E62'  # Traspaso (en los PDFs de prueba, E62 siempre es egreso)
    }
    
    # Códigos de Ingreso (Abonos)
    codigos_abono = {
        'T20', # SPEI Recibido
        'W02', # Deposito de Tercero
        'T22', # SPEI Devuelto
        'E57', # Traspaso (en los PDFs de prueba, E57 siempre es ingreso)
        'Y45', # Compensación
        'F04'  # Venta Fondos de Inversion
    }
    
    if codigo in codigos_cargo:
        return True
    elif codigo in codigos_abono:
        return False
    
    # Fallback para códigos desconocidos
    return True


# --- Funciones de compatibilidad (NO MODIFICAR) ---
def extract_and_normalize_date(date_text):
    return funcion_extraer_fecha_normalizada(date_text)
def extract_amount(amount_text):
    return funcion_extraer_monto(amount_text)
def extract_account_number(text):
    return ""
def extract_reference(text):
    return funcion_extraer_referencia_mejorada([text])
def extract_full_transaction_name(lines):
    return funcion_extraer_nombre_completo_transaccion(lines, 0, "")
def extract_beneficiary_name(lines):
    return funcion_extraer_beneficiario_correcto(lines, '', False)
def create_summarized_name(full_name, trans_type, beneficiary):
    return funcion_crear_nombre_resumido_inteligente(full_name, trans_type, beneficiary, {})
def extract_branch_from_header(text):
    return ""
def classify_transaction(code, desc, column):
    if code in {'T20', 'W02', 'T22', 'E57', 'Y45', 'F04'}:
        return "Ingreso"
    return "Egreso"