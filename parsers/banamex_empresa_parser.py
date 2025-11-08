# Se importaran las bibliotecas de regex y utilidades
import re
from decimal import Decimal
from utils.validators import limpiar_monto

# --- EXPRESIONES REGULARES (REGEX) ESPECIFICAS PARA BANAMEX EMPRESA (INMOVITUR) ---
PATRON_NOMBRE_EMPRESA = re.compile(r"INMOVITUR SA DE CV") #
PATRON_PERIODO = re.compile(r"RESUMEN DEL: (\d{2}/[A-Z]{3}/\d{4}) AL (\d{2}/[A-Z]{3}/\d{4})") #
PATRON_CLABE = re.compile(r"CLABE Interbancaria\s+(\d+)") #
PATRON_SALDO_ANT = re.compile(r"Saldo Anterior\s+([$]?[\d,]+\.\d{2})") #
PATRON_DEPOSITOS = re.compile(r"(\d+) Depósitos\s+([$]?[\d,]+\.\d{2})") #
PATRON_RETIROS = re.compile(r"(\d+) Retiros\s+([$]?[\d,]+\.\d{2})") #
PATRON_SALDO_FINAL = re.compile(r"SALDO AL \d{2} DE [A-Z]+ DE \d{4}\s+([$]?[\d,]+\.\d{2})") #
PATRON_SALDO_PROM = re.compile(r"Saldo Promedio\s+([$]?[\d,]+\.\d{2})") #
PATRON_INICIO_TABLA = re.compile(r"DETALLE DE OPERACIONES") #
PATRON_TRANSACCIONES = re.compile(
    r"^(\d{2}\s[A-Z]{3})\s+(.*?)(?:\s{2,}([\d,]+\.\d{2}))?\s+(?:\s{2,}([\d,]+\.\d{2}))?\s+([\d,.-]+\.\d{2})$",
    re.DOTALL | re.MULTILINE
) #

def parsear_datos_generales(paginas_texto):
    """
    Se extraen los 8 campos de Datos Generales usando regex especificos
    para el formato Banamex Empresa.
    Se recibe lista de paginas como parametro.
    """
    texto_completo = "".join(paginas_texto)
    
    datos = {}
    
    match_nombre = PATRON_NOMBRE_EMPRESA.search(texto_completo)
    match_periodo = PATRON_PERIODO.search(texto_completo)
    match_clabe = PATRON_CLABE.search(texto_completo)
    match_saldo_ant = PATRON_SALDO_ANT.search(texto_completo)
    match_depositos = PATRON_DEPOSITOS.search(texto_completo)
    match_retiros = PATRON_RETIROS.search(texto_completo)
    match_saldo_final = PATRON_SALDO_FINAL.search(texto_completo)
    match_saldo_prom = PATRON_SALDO_PROM.search(texto_completo)
    
    datos['nombre_empresa'] = match_nombre.group(0) if match_nombre else None
    datos['periodo'] = f"DEL {match_periodo.group(1)} AL {match_periodo.group(2)}" if match_periodo else None
    datos['numero_cuenta_clabe'] = match_clabe.group(1) if match_clabe else None
    
    datos['saldo_inicial'] = limpiar_monto(match_saldo_ant.group(1)) if match_saldo_ant else Decimal('0.00')
    datos['total_depositos'] = limpiar_monto(match_depositos.group(2)) if match_depositos else Decimal('0.00')
    datos['total_retiros'] = limpiar_monto(match_retiros.group(2)) if match_retiros else Decimal('0.00')
    datos['saldo_final'] = limpiar_monto(match_saldo_final.group(1)) if match_saldo_final else Decimal('0.00')
    datos['saldo_promedio'] = limpiar_monto(match_saldo_prom.group(1)) if match_saldo_prom else Decimal('0.00')
    
    return datos

def parsear_transacciones(paginas_texto, saldo_inicial):
    """
    Se extraen las transacciones (11 campos) de la tabla de operaciones.
    Se recibe lista de paginas como parametro.
    """
    texto_completo = "".join(paginas_texto)
    
    transacciones = []
    
    match_inicio = PATRON_INICIO_TABLA.search(texto_completo)
    if not match_inicio:
        return []
        
    texto_tabla = texto_completo[match_inicio.end():]
    
    match_fin = re.search(r"Si desea recibir pagos", texto_tabla)
    if match_fin:
        texto_tabla = texto_tabla[:match_fin.start()]
    
    matches = PATRON_TRANSACCIONES.finditer(texto_tabla)
    
    for match in matches:
        fecha_op = match.group(1).strip()
        concepto = match.group(2).strip()
        cargo_str = match.group(3)
        abono_str = match.group(4)
        
        monto = Decimal('0.00')
        clasificacion = None
        
        if cargo_str:
            monto = limpiar_monto(cargo_str)
            clasificacion = "Egreso"
        elif abono_str:
            monto = limpiar_monto(abono_str)
            clasificacion = "Ingreso"
        
        if clasificacion is None:
            continue
            
        transaccion = {
            "fecha": fecha_op,
            "nombre_transaccion": re.sub(r'\s+', ' ', concepto).strip(),
            "nombre_resumido": "", 
            "tipo_transaccion": "", 
            "clasificacion": clasificacion,
            "quien_realiza_o_recibe": "", 
            "monto": monto,
            "numero_referencia_folio": "", 
            "numero_cuenta_origen_destino": "", 
            "metodo_pago": "", 
            "sucursal_o_ubicacion": "" 
        }
        
        transacciones.append(transaccion)
        
    return transacciones