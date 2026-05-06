# Automatizacion de Reportes de Servicios

Aplicacion de escritorio y CLI para diligenciar reportes Word de servicios de forma directa, sin IA.

## Requisitos

1. Tener Microsoft Word instalado.
2. Tener Python 3.14 o compatible.
3. Tener acceso a `C:\Users\Jonathan\Desktop\CLIENTES`.

Cada cliente debe tener una carpeta:

```text
C:\Users\Jonathan\Desktop\CLIENTES\<CLIENTE>\Avance de servicio
```

## Ejecutar la app

```powershell
python -m pip install -r requirements.txt
python -m reporting_assistant.main
```

La app permite:

- seleccionar cliente;
- seleccionar reporte Word existente;
- crear un reporte mensual nuevo;
- diligenciar fecha, horario, horas extra y actividad realizada;
- guardar en Word el texto escrito exactamente por el usuario.

## Crear reportes nuevos

El boton `Crear nuevo reporte` copia una plantilla Word, actualiza el encabezado y prepara el mes con una tabla vacia por cada dia habil.

Si luego se trabaja un sabado, domingo o festivo, la app agrega la tabla de ese dia en el lugar correspondiente.

## CLI directo

```powershell
python -m reporting_assistant.codex_cli `
  --client "PREMEX_GUATEMALA" `
  --report-id "ABRIL" `
  --target-date today `
  --regular-start 08:00 `
  --regular-end 17:30 `
  --rest-minutes 60 `
  --activity-file .\reporte.txt
```

Tambien acepta:

- `--target-date hoy`
- `--target-date yesterday`
- `--target-date ayer`
- `--append` para agregar contenido a una fecha existente.
- `--overtime-segment 17:30-20:30` para horas extra.

## Empaquetar a `.exe`

```powershell
powershell -ExecutionPolicy Bypass -File .\build_report_assistant.ps1
```
