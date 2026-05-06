# Flujo Diario Directo

Este proyecto actualiza reportes Word sin IA.

## Flujo recomendado

1. Seleccione el cliente.
2. Seleccione el reporte Word existente o cree uno nuevo.
3. Seleccione la fecha.
4. Confirme horario, descanso y horas extra.
5. Escriba el texto que debe quedar en `ACTIVIDAD REALIZADA`.
6. Guarde en Word.

## CLI

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

## Reglas operativas

- Lunes a miercoles: `08:00-18:00` menos 60 minutos.
- Jueves y viernes: `08:00-17:30` menos 60 minutos.
- Si hubo horas extra, usar `--overtime-segment` una vez por tramo.
- Si la fecha ya existe y es un complemento, agregar `--append`.
- El texto se guarda directo, sin reescritura automatica.
