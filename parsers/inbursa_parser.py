# Se importaran las bibliotecas de regex y utilidades
import re
from decimal import Decimal
from utils.validators import limpiar_monto

# --- EXPRESIONES REGULARES (REGEX) ESPECIFICAS PARA INBURSA (Inbursact Empresarial) ---
PATRON_NOMBRE_EMPRESA = re.compile(r"Banco\n(.*?)\n", re.DOTALL) #
PATRON_PERIODO = re.compile(r"PERIODO\s+Del (.*?) al (.*?)\n") #
PATRON_CLABE = re.compile(r"CLABE\s+(\d+)") #
PATRON_SALDO_ANT = re.compile(r"SALDO ANTERIOR\s+([\d,.-]+)") #
PATRON_DEPOSITOS = re.compile(r"ABONOS\s+([\d,.-]+)") #
PATRON_RETIROS = re.compile(r"CARGOS\s+([\d,.-]+)") #
PATRON_SALDO_FINAL = re.compile(r"SALDO ACTUAL\s+([\d,.-]+)") #
PATRON_SALDO_PROM = re.compile(r"SALDO PROMEDIO\s+([\d,.-]+)") #
PATRON_INICIO_TABLA = re.compile(r"DETALLE DE MOVIMIENTOS") #

def parsear_datos_generales(paginas_texto):
    """
    Se extraen los 8 campos de Datos Generales usando regex especificos
    para el formato Inbursa Empresarial.
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
    
    datos['nombre_empresa'] = match_nombre.group(1).strip() if match_nombre else None
    datos['periodo'] = f"{match_periodo.group(1)} - {match_periodo.group(2)}" if match_periodo else None
    datos['numero_cuenta_clabe'] = match_clabe.group(1) if match_clabe else None
    
    datos['saldo_inicial'] = limpiar_monto(match_saldo_ant.group(1)) if match_saldo_ant else Decimal('0.00')
    datos['total_depositos'] = limpiar_monto(match_depositos.group(1)) if match_depositos else Decimal('0.00')
    datos['total_retiros'] = limpiar_monto(match_retiros.group(1)) if match_retiros else Decimal('0.00')
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
    texto_tabla = re.sub(r".*BALANCE INICIAL.*?\n", "", texto_tabla)
    
    match_fin = re.search(r"Si desea recibir pagos", texto_tabla)
    if match_fin:
        texto_tabla = texto_tabla[:match_fin.start()]

    patron_bloques = re.compile(
        r"^([A-Z]{3,4}\.\s+\d{2})\s+(.*?)(?=^[A-Z]{3,4}\.\s+\d{2}|\Z)",
        re.MULTILINE | re.DOTALL
    )
    
    for match in patron_bloques.finditer(texto_tabla):
        fecha = match.group(1).strip()
        bloque_completo = match.group(2).strip()
        
        lineas = bloque_completo.split('\n')
        concepto = ' '.join(lineas[:-1]) if len(lineas) > 1 else bloque_completo
        
        match_montos = re.search(r'([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})$', bloque_completo)
        
        if match_montos:
            cargo_str = match_montos.group(1)
            abono_str = match_montos.group(2)
            
            cargo = limpiar_monto(cargo_str)
            abono = limpiar_monto(abono_str)
            
            if cargo > 0:
                monto = cargo
                clasificacion = "Egreso"
            elif abono > 0:
                monto = abono
                clasificacion = "Ingreso"
            else:
                continue
                
            transaccion = {
                "fecha": fecha,
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