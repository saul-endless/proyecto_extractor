# -*- coding: utf-8 -*-
"""
Parser BBVA Empresa v4.0 FINAL
Estructura de salida objetivo con nombres en español
"""

import re
from datetime import datetime
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.field_extractors import (
    extract_and_normalize_date,
    extract_amount,
    extract_account_number,
    extract_reference,
    extract_full_transaction_name,
    extract_beneficiary_name,
    create_summarized_name,
    extract_branch_from_header,
    classify_transaction,
    funcion_extraer_saldo_promedio,
    funcion_limpiar_nombre_empresa,
    funcion_formatear_periodo_archivo,
    funcion_agregar_campos_adicionales_transaccion
)


def parse_bbva_empresa(text_data, ocr_data=None):
    """Parser principal BBVA Empresa."""
    print("\n=== Iniciando Parser BBVA Empresa v4.0 ===")
    
    metadata = extract_metadata(text_data)
    print(f"✓ Metadatos extraídos: {metadata['Nombre de la empresa del estado de cuenta']}")
    print(f"✓ Período: {metadata['Periodo del estado de cuenta']}")
    
    transactions = extract_transactions(text_data, metadata)
    print(f"✓ Transacciones extraídas: {len(transactions)}")
    
    validate_balance(transactions, metadata)
    
    return {
        'metadata': metadata,
        'transactions': transactions
    }


def extract_metadata(text):
    """
    Se extraen los metadatos del estado de cuenta.
    Se devuelve un diccionario con la estructura objetivo.
    """
    # Se inicializa el diccionario de metadatos con valores por defecto
    metadata_raw = {
        'nombre_empresa': '',
        'periodo': '',
        'numero_cuenta_clabe': '',
        'numero_cuenta': '',
        'saldo_inicial': 0.0,
        'total_depositos': 0.0,
        'total_retiros': 0.0,
        'saldo_final': 0.0,
        'saldo_promedio': 0.0,
        'sucursal': '',
        'rfc': '',
        'numero_cliente': ''
    }
    
    lines = text.split('\n')
    
    # Se extrae el nombre de empresa
    for line in lines[:30]:
        if re.search(r'(TECHNOLOGIES|SA DE CV|S\.A\. DE C\.V\.|SOCIEDAD ANONIMA)', line, re.IGNORECASE):
            metadata_raw['nombre_empresa'] = line.strip()
            break
    
    # Se extrae el periodo
    period_match = re.search(r'DEL\s+(\d{2}/\d{2}/\d{4})\s+AL\s+(\d{2}/\d{2}/\d{4})', text, re.IGNORECASE)
    if period_match:
        metadata_raw['periodo'] = f"DEL {period_match.group(1)} AL {period_match.group(2)}"
    
    # Se extrae el numero de cuenta
    account_match = re.search(r'No\.\s*de\s*Cuenta\s+(\d{10})', text, re.IGNORECASE)
    if account_match:
        metadata_raw['numero_cuenta'] = account_match.group(1)
    
    # Se extrae la CLABE
    clabe_match = re.search(r'CLABE\s+(\d{18})', text, re.IGNORECASE)
    if clabe_match:
        metadata_raw['numero_cuenta_clabe'] = clabe_match.group(1)
    
    # Se extrae el RFC
    rfc_match = re.search(r'R\.F\.C\s+([A-Z]{3,4}\d{6}[A-Z0-9]{3})', text, re.IGNORECASE)
    if rfc_match:
        metadata_raw['rfc'] = rfc_match.group(1)
    
    # Se extrae el numero de cliente
    client_match = re.search(r'No\.\s*de\s*Cliente\s+(D\d{7})', text, re.IGNORECASE)
    if client_match:
        metadata_raw['numero_cliente'] = client_match.group(1)
    
    # Se extrae la sucursal
    branch_match = re.search(r'SUCURSAL\s*:\s*(\d{4})', text, re.IGNORECASE)
    if branch_match:
        metadata_raw['sucursal'] = branch_match.group(1)
    
    # Se extraen los datos financieros (saldos y totales)
    extract_financial_data_final(text, metadata_raw)
    
    # Se extrae el saldo promedio usando la nueva funcion
    saldo_promedio = funcion_extraer_saldo_promedio(text)
    metadata_raw['saldo_promedio'] = saldo_promedio
    
    # Se limpia el nombre de la empresa
    nombre_limpio = funcion_limpiar_nombre_empresa(metadata_raw['nombre_empresa'])
    
    # Se formatea el periodo
    periodo_formateado = funcion_formatear_periodo_archivo(metadata_raw['periodo'])
    
    # Se convierte a numeros (no strings)
    saldo_inicial_num = float(metadata_raw.get('saldo_inicial', 0))
    saldo_final_num = float(metadata_raw.get('saldo_final', 0))
    total_depositos_num = float(metadata_raw.get('total_depositos', 0))
    total_retiros_num = float(metadata_raw.get('total_retiros', 0))
    
    # Se construye el diccionario con la estructura objetivo
    metadata_objetivo = {
        "Nombre de la empresa del estado de cuenta": nombre_limpio,
        "Numero de cuenta del estado de cuenta": metadata_raw['numero_cuenta'],
        "Periodo del estado de cuenta": periodo_formateado,
        "Saldo inicial de la cuenta": saldo_inicial_num,
        "Saldo final de la cuenta": saldo_final_num,
        "Saldo promedio del periodo": saldo_promedio,
        "Cantidad total de depositos": total_depositos_num,
        "Cantidad total de retiros": total_retiros_num,
        "Giro de la empresa": "",
        "Sucursal interna": metadata_raw['sucursal']  # ← CAMPO AGREGADO
    }
    
    # Se almacenan valores auxiliares en un diccionario separado
    metadata_objetivo['_auxiliar'] = {
        'periodo_original': metadata_raw['periodo'],
        'sucursal': metadata_raw['sucursal']
    }
    
    return metadata_objetivo


def extract_financial_data_final(text, metadata):
    """
    VERSIÓN FINAL: Extrae montos usando búsqueda robusta.
    Busca el PRIMER monto > $1000 después del título.
    """
    # Buscar sección de comportamiento
    behavior_match = re.search(r'Comportamiento', text, re.IGNORECASE)
    if not behavior_match:
        return
    
    # Trabajar desde la sección de comportamiento
    behavior_section = text[behavior_match.start():]
    lines = behavior_section.split('\n')[:50]  # Primeras 50 líneas
    
    # Saldo Inicial
    for i, line in enumerate(lines):
        if 'SALDO' in line.upper() and 'LIQUIDACI' in line.upper() and 'INICIAL' in line.upper():
            # Buscar monto en las siguientes 3 líneas
            for j in range(i+1, min(i+4, len(lines))):
                amount = extract_amount(lines[j])
                if amount and 0 < amount < 1000000000:
                    metadata['saldo_inicial'] = str(amount)
                    break
            break
    
    # Depósitos / Abonos - BÚSQUEDA ROBUSTA
    for i, line in enumerate(lines):
        if 'DEP' in line.upper() and 'ABONO' in line.upper() and '(+)' in line.upper():
            # Buscar el PRIMER monto > $1000 en las siguientes 5 líneas
            for j in range(i+1, min(i+6, len(lines))):
                amount = extract_amount(lines[j])
                # Debe ser > $1000 y < $10B
                if amount and 1000 < amount < 10000000000:
                    metadata['total_depositos'] = str(amount)
                    break
            break
    
    # Retiros / Cargos - BÚSQUEDA ROBUSTA
    for i, line in enumerate(lines):
        if 'RETIRO' in line.upper() and 'CARGO' in line.upper() and '(-)' in line.upper():
            # Buscar el PRIMER monto > $1000 en las siguientes 5 líneas
            for j in range(i+1, min(i+6, len(lines))):
                amount = extract_amount(lines[j])
                if amount and 1000 < amount < 10000000000:
                    metadata['total_retiros'] = str(amount)
                    break
            break
    
    # Saldo Final
    for i, line in enumerate(lines):
        if 'SALDO FINAL' in line.upper() and '(+)' in line.upper():
            for j in range(i+1, min(i+4, len(lines))):
                amount = extract_amount(lines[j])
                if amount and 0 < amount < 1000000000:
                    metadata['saldo_final'] = str(amount)
                    break
            break


def extract_transactions(text, metadata):
    """Extrae transacciones."""
    movement_start = find_movements_section(text)
    
    if movement_start == -1:
        print("⚠️ ADVERTENCIA: No se encontró 'Detalle de Movimientos'")
        return []
    
    movement_end = text.find('Total de Movimientos', movement_start)
    if movement_end == -1:
        movement_section = text[movement_start:]
    else:
        movement_section = text[movement_start:movement_end]
    
    lines = movement_section.split('\n')
    transaction_groups = group_transaction_lines_fixed(lines)
    print(f"✓ Transacciones agrupadas: {len(transaction_groups)}")
    
    transactions = []
    # Usar valores auxiliares
    sucursal_default = ""  # ← CAMBIO: Dejar vacío
    period_start, period_end = parse_period(metadata.get('_auxiliar', {}).get('periodo_original', ''))
    
    for group in transaction_groups:
        transaction = parse_single_transaction_final(group, sucursal_default, period_start, period_end)
        if transaction:
            transactions.append(transaction)
    
    print(f"✓ Transacciones parseadas exitosamente: {len(transactions)}")
    return transactions


def find_movements_section(text):
    """Encuentra sección de movimientos."""
    patterns = [
        r'DETALLE\s+DE\s+MOVIMIENTOS',
        r'Detalle\s+de\s+Movimientos',
        r'FECHA\s+OPER\s+LIQ'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.start()
    
    return -1


def group_transaction_lines_fixed(lines):
    """Agrupa líneas SIN capturar pie de página."""
    groups = []
    current_group = []
    
    date_pattern = r'^\s*\d{2}/[A-Z]{3}'
    stop_patterns = [
        'INFORMACI[OÓ]N FINANCIERA',
        'ESTADO DE CUENTA',
        'PAGINA',
        'MAESTRA PYME',
        'DOMICILIO FISCAL',
        'MONEDA NACIONAL'
    ]
    
    for line in lines:
        if not line.strip():
            continue
        
        line_upper = line.upper()
        
        if any(re.search(p, line_upper) for p in stop_patterns):
            if current_group:
                groups.append(current_group)
                current_group = []
            continue
        
        if re.match(date_pattern, line):
            if current_group:
                groups.append(current_group)
            current_group = [line]
        else:
            if current_group:
                current_group.append(line)
    
    if current_group:
        groups.append(current_group)
    
    return groups


def parse_single_transaction_final(lines, sucursal_default, period_start, period_end):
    """
    Se parsea una sola transaccion con la estructura objetivo.
    Se devuelve un diccionario con todos los campos requeridos.
    """
    if not lines:
        return None
    
    main_line = lines[0]
    full_text = '\n'.join(lines)
    
    # Se extrae la fecha con año correcto
    fecha_match = re.search(r'(\d{2}/[A-Z]{3})', main_line.upper())
    if not fecha_match:
        return None
    
    fecha_str = fecha_match.group(1)
    
    # Se usa el año del periodo
    if period_start:
        year = period_start.year
        # Se convierte el mes de 3 letras a numero
        month_abbr = fecha_str.split('/')[1]
        month_num = {
            'ENE': '01', 'FEB': '02', 'MAR': '03', 'ABR': '04',
            'MAY': '05', 'JUN': '06', 'JUL': '07', 'AGO': '08',
            'SEP': '09', 'OCT': '10', 'NOV': '11', 'DIC': '12'
        }.get(month_abbr, '01')
        
        day = fecha_str.split('/')[0]
        fecha = f"{day.zfill(2)}-{month_abbr}-{year}"
    else:
        fecha = None
    
    # Se extrae el codigo
    code_match = re.search(r'\b([A-Z]\d{2,3})\b', main_line.upper())
    code = code_match.group(1) if code_match else ""
    
    # Se extrae el nombre completo
    nombre_completo = extract_full_transaction_name(lines)
    
    # Se extrae el beneficiario
    quien = extract_beneficiary_name(lines)
    
    # Se extrae el monto
    monto = extract_transaction_amount_improved(lines, nombre_completo)
    
    if monto is None or monto <= 0:
        return None
    
    # Se determina la columna (ABONOS o CARGOS)
    if code in ['T17', 'A15', 'A14', 'S39', 'S40', 'G30', 'P14', 'N06']:
        amount_column = 'CARGOS'
    elif code in ['T20', 'W02', 'W01']:
        amount_column = 'ABONOS'
    else:
        amount_column = 'ABONOS' if 'RECIBIDO' in nombre_completo.upper() else 'CARGOS'
    
    # Se extraen los campos adicionales
    referencia = extract_reference(full_text)
    cuenta = extract_account_number(full_text)
    tipo, metodo, clasificacion = classify_transaction(code, nombre_completo, amount_column)
    nombre_resumido = create_summarized_name(nombre_completo, tipo, metodo)
    
    # Se construye el diccionario base con la estructura objetivo
    transaccion_base = {
        "Fecha de la transacción": fecha or "",
        "Nombre de la transacción": nombre_completo,
        "Nombre resumido": nombre_resumido,
        "Tipo de transacción": tipo,
        "Clasificación": clasificacion,
        "Quien realiza o recibe el pago": quien,
        "Monto de la transacción": abs(monto),
        "Numero de referencia o folio": referencia,
        "Numero de cuenta origen o destino": cuenta,
        "Metodo de pago": metodo,
        "Sucursal o ubicacion": sucursal_default
    }
    
    # Se agregan los campos adicionales obligatorios
    transaccion_completa = funcion_agregar_campos_adicionales_transaccion(transaccion_base)
    
    return transaccion_completa

def extract_transaction_amount_improved(lines, nombre_completo):
    """
    ULTRA MEJORADO: Detecta el monto REAL evitando saldos y números incorrectos.
    
    Reglas:
    - Números > $500,000 = saldos (ignorar)
    - Si hay número < 100 seguido de monto > 100 → tomar el segundo
    - Evitar IDs y CLABEs
    """
    for line in lines:
        line_clean = line.strip()
        
        # SKIP líneas problemáticas
        if re.search(r'\d{18}', line_clean):  # CLABE
            continue
        if re.search(r'[#\-][A-Z]*(\d{7,})', line_clean):  # IDs
            continue
        
        # Encontrar TODOS los montos con formato X,XXX.XX o XXX.XX
        amounts = re.findall(r'([\d,]+\.\d{2})', line_clean)
        
        if amounts:
            # Convertir todos a float
            parsed_amounts = []
            for amt_str in amounts:
                amt = extract_amount(amt_str)
                if amt and 0.01 <= amt <= 1000000:  # Rango válido
                    parsed_amounts.append(amt)
            
            if not parsed_amounts:
                continue
            
            # ESTRATEGIA INTELIGENTE:
            # 1. Filtrar saldos (> $500,000)
            non_balance_amounts = [a for a in parsed_amounts if a < 500000]
            
            if not non_balance_amounts:
                # Si todos son > 500k, tomar el más pequeño
                return min(parsed_amounts)
            
            # 2. Si hay número < 100, tomar el SIGUIENTE monto
            for i, amt in enumerate(non_balance_amounts):
                if amt < 100 and i + 1 < len(non_balance_amounts):
                    # Hay un número pequeño seguido de otro monto
                    next_amt = non_balance_amounts[i + 1]
                    if next_amt >= 100:  # El siguiente es razonable
                        return next_amt
            
            # 3. Tomar el monto más grande < $500k
            return max(non_balance_amounts)
        
        # Fallback: números sin decimales (comisiones pequeñas)
        simple_match = re.search(r'(\d{1,5})\s*$', line_clean)
        if simple_match:
            amount = extract_amount(simple_match.group(1))
            if amount and 0.01 <= amount <= 10000:
                return amount
    
    return None


def parse_period(period_str):
    """Convierte período a datetime."""
    if not period_str:
        return None, None
    
    match = re.search(r'DEL\s+(\d{2})/(\d{2})/(\d{4})\s+AL\s+(\d{2})/(\d{2})/(\d{4})', period_str)
    if not match:
        return None, None
    
    try:
        start = datetime(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        end = datetime(int(match.group(6)), int(match.group(5)), int(match.group(4)))
        return start, end
    except:
        return None, None


def validate_balance(transactions, metadata):
    """Valida balance."""
    print("\n=== Validando Balance ===")
    
    total_ingresos = sum(t['Monto de la transacción'] for t in transactions if t['Clasificación'] == 'Ingreso')
    total_egresos = sum(t['Monto de la transacción'] for t in transactions if t['Clasificación'] == 'Egreso')
    
    declarado_depositos = float(metadata.get('Cantidad total de depositos', 0))
    declarado_retiros = float(metadata.get('Cantidad total de retiros', 0))
    
    diff_depositos = abs(total_ingresos - declarado_depositos)
    diff_retiros = abs(total_egresos - declarado_retiros)
    
    print(f"Depósitos - Declarado: ${declarado_depositos:,.2f} | Calculado: ${total_ingresos:,.2f} | Diff: ${diff_depositos:,.2f}")
    print(f"Retiros   - Declarado: ${declarado_retiros:,.2f} | Calculado: ${total_egresos:,.2f} | Diff: ${diff_retiros:,.2f}")
    
    tolerance = 100.00
    
    if diff_depositos > tolerance:
        print(f"⚠️ ERROR: Diferencia en depósitos superior a tolerancia")
    else:
        print("✓ Depósitos validados correctamente")
    
    if diff_retiros > tolerance:
        print(f"⚠️ ERROR: Diferencia en retiros superior a tolerancia")
    else:
        print("✓ Retiros validados correctamente")