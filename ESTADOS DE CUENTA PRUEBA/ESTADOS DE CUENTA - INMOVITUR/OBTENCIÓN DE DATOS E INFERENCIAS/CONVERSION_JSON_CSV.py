import json
import csv
import os

def convertir_json_a_csv(ruta_json):
    """
    Convierte un archivo JSON a CSV con las columnas especificadas
    """
    # Definir las columnas en el orden deseado
    columnas = [
        "Fecha de la transacción",
        "Nombre de la transacción", 
        "Nombre resumido",
        "Tipo de transacción",
        "Clasificación",
        "Quien realiza o recibe el pago",
        "Monto de la transacción",
        "Numero de referencia o folio",
        "Numero de cuenta origen o destino",
        "Metodo de pago",
        "Sucursal o ubicacion",
        "Giro de la transacción"
    ]
    
    # Leer el archivo JSON
    with open(ruta_json, 'r', encoding='utf-8') as archivo_json:
        datos = json.load(archivo_json)
    
    # Crear la ruta del archivo CSV (mismo nombre, misma ubicación)
    directorio = os.path.dirname(ruta_json)
    nombre_archivo = os.path.splitext(os.path.basename(ruta_json))[0]
    ruta_csv = os.path.join(directorio, f"{nombre_archivo}.csv")
    
    # Escribir el archivo CSV
    with open(ruta_csv, 'w', newline='', encoding='utf-8') as archivo_csv:
        escritor = csv.DictWriter(archivo_csv, fieldnames=columnas)
        
        # Escribir la cabecera
        escritor.writeheader()
        
        # Si datos es una lista de transacciones
        if isinstance(datos, list):
            for transaccion in datos:
                escritor.writerow(transaccion)
        # Si datos es una sola transacción
        else:
            escritor.writerow(datos)
    
    print(f"Archivo guardado en: {ruta_csv}")

def main():    
    ruta = input("Ruta del archivo JSON: ")
    convertir_json_a_csv(ruta)
                
if __name__ == "__main__":
    main()