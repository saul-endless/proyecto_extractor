#!/bin/bash
# reparar_entorno.sh - VERSIÓN CORREGIDA Y ROBUSTA PARA RTX 5090

echo ">>> [1/3] Verificando instalación de PyTorch..."
# Ya lo instalaste, pero verificamos que sea la version cu128
PIP_LIST=$(pip list | grep "torch .*cu128")

if [ -z "$PIP_LIST" ]; then
    echo "   (!) No detecto la versión cu128. Reinstalando forzadamente..."
    pip uninstall -y torch torchvision torchaudio
    pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128
else
    echo "   (OK) PyTorch cu128 ya está instalado."
fi

# Intentar instalar flash-attn (opcional pero recomendado para Qwen, aunque difícil de compilar en nightly)
# Si falla, el código Python usará 'sdpa' automáticamente.
echo ">>> [Intento opcional] Instalando dependencias de optimización..."
pip install --no-deps packaging ninja
# No forzamos flash-attn aquí para no romper el script si falla la compilación.

echo ">>> [2/3] Buscando directorio de NVRTC (El compilador de CUDA)..."

# Método infalible: Buscar el archivo real en el entorno de conda/python actual
# Buscamos libnvrtc.so.12.* dentro del directorio actual de Python
SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])")
NVRTC_DIR=$(find "$SITE_PACKAGES" -name "libnvrtc.so.12*" -print -quit | xargs dirname)

if [ -d "$NVRTC_DIR" ]; then
    echo "   (OK) Directorio encontrado: $NVRTC_DIR"
    cd "$NVRTC_DIR"

    echo ">>> [3/3] Aplicando parche de enlaces simbólicos (Symlinks)..."
    
    # Buscar el archivo 'builtins' real. Puede llamarse .12.8.93, .12.9, etc.
    # El sort -V asegura que agarramos la versión más alta si hay varias.
    REAL_FILE=$(ls libnvrtc-builtins.so.12* 2>/dev/null | sort -V | tail -n 1)

    if [ ! -z "$REAL_FILE" ]; then
        echo "   -> Archivo fuente detectado: $REAL_FILE"
        
        # Crear los enlaces que busca PyTorch desesperadamente
        ln -sf "$REAL_FILE" libnvrtc-builtins.so
        ln -sf "$REAL_FILE" libnvrtc-builtins.so.12.8
        
        echo "   -> Enlaces creados:"
        ls -l libnvrtc-builtins.so*
        
        # También arreglamos libnvrtc.so si falta
        if [ ! -f "libnvrtc.so" ]; then
            REAL_NVRTC=$(ls libnvrtc.so.12* | grep -v builtins | head -n 1)
            ln -sf "$REAL_NVRTC" libnvrtc.so
            echo "   -> Enlace libnvrtc.so creado apuntando a $REAL_NVRTC"
        fi
        
        echo ">>> ¡EXITO! El entorno ha sido reparado para la RTX 5090."
    else
        echo ">>> [ERROR] No encontré 'libnvrtc-builtins.so.*' en $NVRTC_DIR"
        echo "    Es posible que el paquete 'nvidia-cuda-nvrtc-cu12' esté corrupto."
        echo "    Intenta: pip install --force-reinstall nvidia-cuda-nvrtc-cu12==12.8.93"
    fi
else
    echo ">>> [ERROR] No pude encontrar la carpeta de librerías Nvidia en $SITE_PACKAGES"
    echo "    Asegúrate de estar en el entorno virtual correcto (python_env)."
fi