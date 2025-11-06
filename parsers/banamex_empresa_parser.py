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

def parsear_datos_generales(texto_completo):
    """
    Se extraeran los 8 campos de Datos Generales usando regex especificos
    para el formato Banamex Empresa.
    """
    # Se inicializara el diccionario de datos
    datos = {}
    
    # Se buscaran los patrones en el texto
    match_nombre = PATRON_NOMBRE_EMPRESA.search(texto_completo)
    match_periodo = PATRON_PERIODO.search(texto_completo)
    match_clabe = PATRON_CLABE.search(texto_completo)
    match_saldo_ant = PATRON_SALDO_ANT.search(texto_completo)
    match_depositos = PATRON_DEPOSITOS.search(texto_completo)
    match_retiros = PATRON_RETIROS.search(texto_completo)
    match_saldo_final = PATRON_SALDO_FINAL.search(texto_completo)
    match_saldo_prom = PATRON_SALDO_PROM.search(texto_completo)
    
    # Se asignaran los valores encontrados
    datos['nombre_empresa'] = match_nombre.group(0) if match_nombre else None
    datos['periodo'] = f"{match_periodo.group(1)} - {match_periodo.group(2)}" if match_periodo else None
    datos['numero_cuenta_clabe'] = match_clabe.group(1) if match_clabe else None
    
    # Se limpiaran los montos
    datos['saldo_inicial'] = limpiar_monto(match_saldo_ant.group(1)) if match_saldo_ant else Decimal('0.00')
    datos['total_depositos'] = limpiar_monto(match_depositos.group(2)) if match_depositos else Decimal('0.00')
    datos['total_retiros'] = limpiar_monto(match_retiros.group(2)) if match_retiros else Decimal('0.00')
    datos['saldo_final'] = limpiar_monto(match_saldo_final.group(1)) if match_saldo_final else Decimal('0.00')
    datos['saldo_promedio'] = limpiar_monto(match_saldo_prom.group(1)) if match_saldo_prom else Decimal('0.00')
    
    # Se retornaran los datos generales
    return datos

def parsear_transacciones(texto_completo, saldo_inicial):
    """
    Se extraeran las transacciones (11 campos) de la tabla de operaciones.
    Este parser usa un regex multilinea para capturar bloques.
    """
    # Se inicializara la lista
    transacciones = []
    
    # Se encontrara el inicio de la tabla
    match_inicio = PATRON_INICIO_TABLA.search(texto_completo)
    if not match_inicio:
        return []
        
    # Se cortara el texto para empezar desde la tabla
    texto_tabla = texto_completo[match_inicio.end():]
    
    # Se encontrara el fin de la tabla
    match_fin = re.search(r"SALDO MINIMO REQUERIDO", texto_tabla)
    if match_fin:
        texto_tabla = texto_tabla[:match_fin.start()]
    
    # Se buscaran todas las coincidencias de transacciones
    matches = PATRON_TRANSACCIONES.finditer(texto_tabla)
    
    # Se iterara sobre cada match encontrado
    for match in matches:
        # Se extraerán los grupos del regex
        fecha = match.group(1).strip()
        concepto = match.group(2).strip()
        retiro_str = match.group(3)
        deposito_str = match.group(4)
        
        # Se determinara el monto y la clasificacion
        monto = Decimal('0.00')
        clasificacion = None
        
        if retiro_str:
            # Se asignara como Egreso
            monto = limpiar_monto(retiro_str)
            clasificacion = "Egreso"
        elif deposito_str:
            # Se asignara como Ingreso
            monto = limpiar_monto(deposito_str)
            clasificacion = "Ingreso"
        
        # Se saltaran lineas sin monto (ej. 'SALDO ANTERIOR')
        if clasificacion is None:
            continue
            
        # Se construira el objeto de transaccion
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
        
        # Se agregara la transaccion a la lista
        transacciones.append(transaccion)
        
    # Se retornara la lista de transacciones
    return transacciones