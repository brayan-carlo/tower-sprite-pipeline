# Contrato de PNG genérico con frames secuenciales

Leer este archivo completo cuando la entrada sea un PNG genérico de un personaje aislado. No usarlo para `sequential_reconstruction`, el piloto del ogro ni las rutas deterministas o híbridas existentes.

## Eligibility

La entrada debe ser un PNG decodificable con un personaje aislado y píxeles visibles. Se admite transparencia existente o un borde opaco de un único color uniforme que el normalizador pueda retirar de forma exacta. Si el borde es complejo, la entrada queda `INPUT_BACKGROUND_COMPLEX`; no adivinar un recorte ni eliminar fondo con otra herramienta.

Antes de preparar, obtener de forma explícita:

- ruta del PNG fuente;
- animación;
- dirección;
- cantidad entera de frames;
- canvas de salida: dimensiones `width` y `height`, o la instrucción explícita de conservar el canvas de la fuente;
- presupuesto máximo en USD para toda la corrida.

No pedir campos legacy como modelos alternativos, ancla o ruta de Unity. El modelo, fondo transparente, ancla bottom-center y remuestreo nearest pertenecen al contrato implementado. La disponibilidad live del modelo y de su precio debe verificarse por separado.

El operador elige un workspace externo seguro y un `run_id` nuevo cuando el usuario no los fija. No preguntar por esas decisiones operativas salvo que una restricción del usuario cambie su ubicación o alcance. Construir un movimiento concreto por frame a partir de la animación, dirección y cantidad solicitadas. Cada entrada lleva `frame_index`, `phase` y `body_motion`; el usuario lo revisa dentro del resumen completo. Si el solicitante dice que revisará o autorizará, esa persona es el revisor de la corrida: no inventar su nombre ni exigir un aprobador legacy.

Una corrida abarca exactamente una animación y una dirección. Si falta `frame_count`, presupuesto, animación, dirección o canvas, pedir solo el dato faltante antes de preparar. Una duración pedida distinta de 100 ms no bloquea la preparación si el usuario acepta la limitación: informar que esta versión produce 100 ms por frame y no afirmar que respeta otra duración.

## Parámetros y staging

Resolver runtimes sin instalar junto a la skill:

```powershell
$pipelineHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else {
    Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex'
}
$skillRoot = Join-Path $pipelineHome 'skills\tower-sprite-pipeline'
$runtimeRoot = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.cache\codex-runtimes\codex-primary-runtime\dependencies'
$python = Join-Path $runtimeRoot 'python\python.exe'
$node = Join-Path $runtimeRoot 'node\bin\node.exe'
$pipeline = Join-Path $skillRoot 'scripts\generic_sprite_pipeline.py'
$adapter = Join-Path $skillRoot 'scripts\fal_generic_generate.mjs'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:TOWER_SPRITE_PYTHON_EXE = $python
$env:TOWER_SPRITE_NODE_EXE = $node
& $python -B -c "import sys, PIL; print(sys.version); print(PIL.__version__)"
if ($LASTEXITCODE -ne 0) { throw 'ENVIRONMENT_BLOCKED:PYTHON_IMPORT' }
& $node --version
if ($LASTEXITCODE -ne 0) { throw 'ENVIRONMENT_BLOCKED:NODE' }
```

Asignar `$sourcePath`, `$movementPlanPath`, `$pricingPath`, `$workspaceRoot`, `$runId`, `$animation`, `$direction`, `$frameCount` y `$maxBudgetUsd` con los valores de la corrida. El directorio de corrida todavía no debe existir. Mantener la fuente fuera de `$workspaceRoot` y el workspace fuera del proyecto del juego.

El archivo de movimiento es un array JSON con exactamente una entrada por frame, en orden:

```json
[
  {
    "frame_index": 1,
    "phase": "contact",
    "body_motion": "right foot forward, left arm forward, torso leaning slightly into the run"
  }
]
```

La descripción de `body_motion` debe representar el movimiento pedido, no una etiqueta vacía o genérica. El manifest v2 y el resumen vinculan el prompt completo de cada frame.

## Precio antes de preparar

El adaptador implementa el origen canónico `https://api.fal.ai/v1/models/pricing`, el modelo `fal-ai/nano-banana-2/edit`, unidad `image` y resolución `0.5K`. Son identificadores del contrato de código; no constituyen una cotización ni confirman disponibilidad live.

Para una corrida live, obtener evidencia autenticada vigente antes de preparar, sin transmitir el PNG:

```powershell
$pricingOutput = & $node $adapter pricing --source 'https://api.fal.ai/v1/models/pricing' --output $pricingPath
if ($LASTEXITCODE -ne 0) { throw 'PRICE_NOT_VERIFIABLE' }
$pricingResult = $pricingOutput | ConvertFrom-Json
$pricingReceiptPath = $pricingPath + '.provenance.json'
$pricingReceipt = Get-Content -Raw -LiteralPath $pricingReceiptPath | ConvertFrom-Json
$pricingHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $pricingPath).Hash.ToLowerInvariant()
$receiptHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $pricingReceiptPath).Hash.ToLowerInvariant()
if ($pricingReceipt.pricing_sha256 -ne $pricingHash -or
    $pricingResult.provenance.sha256 -ne $receiptHash) {
    throw 'PRICE_PROVENANCE_INVALID'
}
$pricingReceipt | ConvertTo-Json -Depth 12
```

Este comando requiere `FAL_KEY` en el entorno y consulta metadata de precios. No imprimir la credencial. La tarifa base procede de la API autenticada del modelo exacto; las unidades `image` e `images` se normalizan a `image`. Otras unidades o monedas quedan bloqueadas.

Si la API proporciona un multiplicador válido para `resolution_multipliers["0.5K"]`, se usa ese dato. Si lo omite, el adaptador consulta exclusivamente `https://fal.ai/models/fal-ai/nano-banana-2/edit`, sin enviar la credencial, y extrae el factor explícito de su párrafo de precios. La página debe identificar el modelo canónico y una única tarifa inequívoca; su precio base debe coincidir con el de la API. Un factor suministrado pero inválido no se reemplaza por el de la página. Datos ausentes, ambiguos, contradictorios, HTTP fallido o redirección mantienen el bloqueo. No inventar multiplicador ni usar un valor por defecto. Una tarifa particular de cuenta que discrepe de la página también queda bloqueada hasta resolver esa discrepancia.

La salida de `pricing` conserva las ocho claves admitidas por `prepare`. El archivo adyacente `$pricingPath.provenance.json` identifica por separado las URLs, instantes de consulta, hashes de respuestas y datos seleccionados, y vincula el recibo al SHA-256 de los bytes exactos del precio. `source_url` del precio identifica la API de la tarifa base; no significa que el factor de la página viniera en esa respuesta. Mostrar el recibo completo junto al resumen antes de pedir autorización. Es evidencia de auditoría; no es otro input de `prepare`/`execute` ni modifica un manifest histórico.

Ambos destinos deben ser nuevos. El recibo se publica primero; si después falla la publicación del precio, conservar el recibo huérfano y usar otro destino para la siguiente consulta. No reutilizarlo, borrarlo ni presentar éxito sin los dos archivos. El comprobador PowerShell anterior verifica los hashes de los artefactos; su error `PRICE_PROVENANCE_INVALID` no es un subcomando del adaptador.

La resolución del proveedor es `0.5K`; un canvas final mayor describe el tamaño del PNG entregado, no detalle nativo generado a esa resolución. Informarlo junto al resumen si se conserva una fuente más grande.

La evidencia debe usar fecha ASCII RFC3339 válida con zona explícita. Al preparar y antes de cada submit nuevo, pasan edades de hasta 900 segundos inclusive y adelantos de hasta 60 segundos inclusive, medidos con precisión de milisegundos. Una evidencia más vieja o más futura bloquea gasto nuevo.

Una simulación offline solo procede si el usuario la pidió. El comando siguiente valida de forma aislada el camino `pricing` con un fixture bajo una raíz falsa confinada y produce una salida marcada:

```powershell
$env:NODE_ENV = 'test'
$env:FAKE_ROOT = $offlineRoot
$env:FAL_KEY = ''
& $node $adapter pricing --source $offlinePricingFixture --output $markedOfflinePricingPath
if ($LASTEXITCODE -ne 0) { throw 'OFFLINE_PRICE_FIXTURE_INVALID' }
```

Conservar `$markedOfflinePricingPath` como evidencia del test aislado. No entregarlo a `prepare`: contiene `evidence_mode` y el contrato estricto de preparación no admite esa clave. No retirar la marca para forzar aceptación.

Para preparar dentro del harness offline, crear por separado un input sintético de exactamente ocho claves dentro de `$offlineRoot` y una declaración externa que lo marque como simulación. Los valores `0.10` y `0.5` de este ejemplo son ficticios del harness C4, no defaults ni precios live:

```powershell
$harnessPricingPath = Join-Path $offlineRoot 'pricing-input-synthetic.json'
$offlineDeclarationPath = Join-Path $offlineRoot 'offline-declaration.json'
$verifiedAt = [DateTimeOffset]::UtcNow.ToUniversalTime().ToString(
    "yyyy-MM-dd'T'HH:mm:ss.fff'Z'",
    [Globalization.CultureInfo]::InvariantCulture
)
$harnessPricing = [ordered]@{
    model = 'fal-ai/nano-banana-2/edit'
    source_url = 'https://api.fal.ai/v1/models/pricing'
    verified_at = $verifiedAt
    currency = 'USD'
    unit = 'image'
    base_unit_price_usd = '0.10'
    resolution = '0.5K'
    resolution_multiplier = '0.5'
}
$utf8NoBom = [Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText(
    $harnessPricingPath,
    ($harnessPricing | ConvertTo-Json -Compress),
    $utf8NoBom
)
$pricingSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $harnessPricingPath).Hash.ToLowerInvariant()
$offlineDeclaration = [ordered]@{
    evidence_mode = 'OFFLINE_FIXTURE_NOT_FOR_LIVE_USE'
    purpose = 'synthetic pricing input for an explicitly requested offline simulation'
    pricing_sha256 = $pricingSha256
}
[IO.File]::WriteAllText(
    $offlineDeclarationPath,
    ($offlineDeclaration | ConvertTo-Json -Compress),
    $utf8NoBom
)
$pricingPath = $harnessPricingPath
```

El JSON sintético satisface la interfaz existente de `prepare`. La declaración externa identifica la simulación; el adaptador no la interpreta como un gate técnico. La prohibición operativa es explícita: nunca usar ese JSON para live ni presentarlo como cotización. Mantener ambos archivos y sus hashes en el informe offline.

Si la simulación termina en preparación/resumen, no cargar cliente ni ejecutar transporte. Para continuar una simulación autorizada hasta `execute`, crear una raíz nueva: no volver a ejecutar el runner C4 ni reutilizar su directorio o sus eventos históricos. El harness prepara dentro de la raíz nueva una copia del `fake-client.mjs` existente, un PNG de resultado falso, archivos de eventos propios y logs nuevos. Todos se registran con hash en el informe offline.

El cliente falso C4 requiere `FAKE_EVENTS_PATH` y `FAKE_ACTIONS_PATH`. El adaptador requiere además una `FAL_KEY` no vacía aun con `--client-module`; usar `fake-key-never-persist` únicamente en el entorno temporal del proceso de prueba. El entorno exterior debe empezar sin clave real y se restaura en `finally`.

Preparar una función confinada para cada invocación:

```powershell
if (-not [string]::IsNullOrEmpty($env:FAL_KEY)) {
    throw 'OFFLINE_OUTER_FAL_KEY_MUST_BE_EMPTY'
}
$offlineRootResolved = (Resolve-Path -LiteralPath $offlineRoot).Path.TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
)
$offlinePrefix = $offlineRootResolved + [IO.Path]::DirectorySeparatorChar

function Assert-InOfflineRoot([string]$candidate, [bool]$mustExist = $true) {
    $full = if ($mustExist) {
        (Resolve-Path -LiteralPath $candidate).Path
    } else {
        [IO.Path]::GetFullPath($candidate)
    }
    if (-not $full.StartsWith($offlinePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "OFFLINE_PATH_OUTSIDE_ROOT:$candidate"
    }
}

function Invoke-OfflineExecute([string]$eventsPath, [string]$actionsPath) {
    foreach ($existing in @(
        $fakeClientModule, $eventsPath, $runManifest,
        $requestPlan, $pricingPath, $python, $node, $adapter
    )) {
        if (-not (Test-Path -LiteralPath $existing -PathType Leaf)) {
            throw "OFFLINE_INPUT_MISSING:$existing"
        }
    }
    foreach ($confined in @(
        $fakeClientModule, $eventsPath, $runManifest,
        $requestPlan, $pricingPath
    )) {
        Assert-InOfflineRoot $confined
    }
    Assert-InOfflineRoot $actionsPath $false
    if (Test-Path -LiteralPath $actionsPath) {
        throw "OFFLINE_ACTIONS_LOG_MUST_BE_NEW:$actionsPath"
    }

    $names = @(
        'NODE_ENV', 'FAKE_ROOT', 'FAKE_EVENTS_PATH',
        'FAKE_ACTIONS_PATH', 'TOWER_SPRITE_PYTHON_EXE', 'FAL_KEY'
    )
    $saved = @{}
    $present = @{}
    foreach ($name in $names) {
        $present[$name] = Test-Path -LiteralPath "Env:$name"
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
    try {
        $env:NODE_ENV = 'test'
        $env:FAKE_ROOT = $offlineRootResolved
        $env:FAKE_EVENTS_PATH = $eventsPath
        $env:FAKE_ACTIONS_PATH = $actionsPath
        $env:TOWER_SPRITE_PYTHON_EXE = $python
        $env:FAL_KEY = 'fake-key-never-persist'
        & $node $adapter execute `
            --run-manifest $runManifest `
            --request-plan $requestPlan `
            --pricing $pricingPath `
            --client-module $fakeClientModule
        $executeCode = $LASTEXITCODE
    } finally {
        foreach ($name in $names) {
            if ($present[$name]) {
                [Environment]::SetEnvironmentVariable(
                    $name,
                    $saved[$name],
                    'Process'
                )
            } else {
                Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
            }
        }
    }
    if ($executeCode -ne 0) {
        throw "OFFLINE_EXECUTE_FAILED:$executeCode"
    }
}
```

Para crear una solicitud falsa nueva, el archivo de eventos empieza con `submit` y luego `status`. El ID es ficticio y queda persistido por el transporte:

```powershell
$offlineRequestId = 'offline-frame-01-request'
$submitEventsPath = Join-Path $offlineRoot 'events-frame-01-submit.json'
$submitActionsPath = Join-Path $offlineRoot 'actions-frame-01-submit.log'
$submitEvents = @(
    [ordered]@{
        operation = 'submit'
        value = [ordered]@{ request_id = $offlineRequestId }
    },
    [ordered]@{
        operation = 'status'
        value = [ordered]@{ status = 'IN_QUEUE' }
    }
)
[IO.File]::WriteAllText(
    $submitEventsPath,
    ($submitEvents | ConvertTo-Json -Depth 8 -Compress),
    $utf8NoBom
)
Invoke-OfflineExecute $submitEventsPath $submitActionsPath
```

Para reanudar ese mismo ID, usar otro archivo que contenga solo `status` y `result`; nunca repetir el evento `submit`. `$offlineResultPng` es un PNG falso ya creado dentro de la raíz nueva:

```powershell
$resumeEventsPath = Join-Path $offlineRoot 'events-frame-01-resume.json'
$resumeActionsPath = Join-Path $offlineRoot 'actions-frame-01-resume.log'
Assert-InOfflineRoot $offlineResultPng
$offlineResultDataUri = 'data:image/png;base64,' + [Convert]::ToBase64String(
    [IO.File]::ReadAllBytes($offlineResultPng)
)
$resumeEvents = @(
    [ordered]@{
        operation = 'status'
        value = [ordered]@{ status = 'COMPLETED' }
    },
    [ordered]@{
        operation = 'result'
        value = [ordered]@{
            data = [ordered]@{
                images = @([ordered]@{ url = $offlineResultDataUri })
            }
        }
    }
)
[IO.File]::WriteAllText(
    $resumeEventsPath,
    ($resumeEvents | ConvertTo-Json -Depth 8 -Compress),
    $utf8NoBom
)
Invoke-OfflineExecute $resumeEventsPath $resumeActionsPath

$runRecord = Get-Content -Raw -LiteralPath $runManifest | ConvertFrom-Json
$requestRecord = $runRecord.requests | Where-Object {
    $_.frame_index -eq $frameIndex
}
if ($requestRecord.request_id -ne $offlineRequestId) {
    throw 'OFFLINE_REQUEST_ID_CHANGED'
}
if ((Get-Content -LiteralPath $submitActionsPath) -notcontains 'submit') {
    throw 'OFFLINE_SUBMIT_NOT_OBSERVED'
}
if ((Get-Content -LiteralPath $resumeActionsPath) -contains 'submit') {
    throw 'OFFLINE_RESUME_RESUBMITTED'
}
if (-not [string]::IsNullOrEmpty($env:FAL_KEY)) {
    throw 'OFFLINE_OUTER_FAL_KEY_NOT_RESTORED'
}
```

El resultado falso usa `value.data.images`, igual que el cliente C4. La receta no hace HTTP: `--client-module` inyecta el cliente confinado. En ningún caso verifica precio, disponibilidad, tráfico o facturación reales.

## Preparación y manifest

Si el canvas de salida es el canvas de la fuente, omitir ambos flags de dimensiones:

```powershell
$runDir = & $python -B $pipeline prepare `
    --source $sourcePath `
    --movement-plan $movementPlanPath `
    --pricing $pricingPath `
    --animation $animation `
    --direction $direction `
    --frame-count $frameCount `
    --max-budget-usd $maxBudgetUsd `
    --workspace-root $workspaceRoot `
    --run-id $runId
if ($LASTEXITCODE -ne 0) { throw 'PREPARE_FAILED' }
```

Para un canvas explícito, añadir juntos `--frame-width $frameWidth --frame-height $frameHeight`. No se admite solo una dimensión.

`prepare` crea una corrida v2 en estado `DRAFT`, copia la fuente y guarda el master normalizado, el movimiento y `reports/manifest.json` como contrato inmutable. `reports/run_manifest.json` es el ledger mutable del lifecycle y solo cambia mediante los comandos del pipeline. No envía imágenes ni llama al proveedor. Nunca reutilizar `$runId` ni un directorio existente.

Las parejas válidas son manifest/run manifest 1/1 y 2/2. V2 exige `provider.prompt_template_version: 2` y agrega `body_motion` al prompt. V1 conserva literalmente su prompt histórico sin `body_motion`. No migrar ni reconstruir una corrida v1 existente.

## Resumen y autorización

```powershell
$runManifest = Join-Path $runDir 'reports\run_manifest.json'
& $python -B $pipeline summary --run-manifest $runManifest
if ($LASTEXITCODE -ne 0) { throw 'SUMMARY_FAILED' }
```

Presentar el JSON completo antes de pedir autorización. Debe mostrar:

- archivo, ruta y SHA-256 originales;
- ruta y SHA-256 del master que se transmitiría;
- animación, dirección, cantidad y dimensiones;
- movimiento completo de cada frame;
- en v2, `schema_version`, `prompt_template_version` y `effective_prompts` completos;
- proveedor/modelo declarados por el manifest;
- precio efectivo por imagen, cantidad planificada, peor caso y presupuesto máximo;
- junto al JSON, la duración efectiva fija de 100 ms por frame, porque el resumen actual no contiene un campo de duración.

Para precio live, adjuntar también el recibo completo `.provenance.json` ya comprobado. La tarifa base y el factor deben atribuirse a sus respectivas fuentes; el recibo no se inserta como una clave nueva del JSON del resumen.

No transmitir el PNG ni autorizar en nombre del usuario. Después de que la persona identificada apruebe ese resumen y ese máximo exacto:

```powershell
& $python -B $pipeline authorize `
    --run-manifest $runManifest `
    --authorized-budget-usd $maxBudgetUsd
if ($LASTEXITCODE -ne 0) { throw 'AUTHORIZATION_FAILED' }
```

Una modificación de fuente, movimiento, prompt, versión, cantidad, dimensiones o presupuesto requiere otra corrida y otra autorización.

## Provider lifecycle

Crear el plan de la siguiente solicitud dentro de `input/requests`:

```powershell
$requestPlan = Join-Path $runDir ('input\requests\frame-{0:D2}.json' -f $frameIndex)
& $python -B $pipeline next-request --run-manifest $runManifest --output $requestPlan
if ($LASTEXITCODE -ne 0) { throw 'NEXT_REQUEST_FAILED' }
```

El resultado JSON `null` significa que no se produjo otra solicitud. Inspeccionar el estado y `terminal_reason` de la corrida para distinguir finalización, rechazo o bloqueo; `next-request` no llama a Fal ni inventa éxito.

Para ejecutar el plan live, el adaptador debe estar en una copia byte por byte de la skill aprobada bajo el runtime externo que contiene `@fal-ai/client` 1.10.1. Verificar inventario y SHA-256 de cada archivo contra el snapshot aprobado antes de usarla. Cambiar solo el `cwd` no resuelve el import ESM ni los helpers Python adyacentes. No instalar el SDK dentro de la skill y no usar `--client-module` en live.

Después de apuntar `$liveAdapter` a esa copia exacta:

```powershell
& $node $liveAdapter execute `
    --run-manifest $runManifest `
    --request-plan $requestPlan `
    --pricing $pricingPath
$executeCode = $LASTEXITCODE
```

Interpretar la salida antes de continuar:

- Con request ID conocido y estado pendiente, repetir el mismo comando para consultar `status`/`result`; reanuda el mismo ID y no hace otro submit.
- `EVALUATION_REQUIRED` indica que el raw quedó persistido y debe evaluarse.
- `SUBMISSION_STATUS_UNKNOWN` sin ID deja la corrida `BLOCKED` y conserva el claim. No volver a enviar, borrar el claim ni fabricar un ID.
- Un estado remoto terminal o un error de contrato detiene la secuencia.

En la ruta live genérica, el cliente creado para una ejecución admite como máximo una delegación POST al endpoint exacto `https://queue.fal.run/fal-ai/nano-banana-2/edit`, fuerza `redirect: "error"`, bloquea otro POST y permite GET de consulta. El claim y el ledger impiden reenvío entre comandos cuando falta ID. Esta garantía termina en la frontera SDK/fetch de `@fal-ai/client` 1.10.1: las pruebas offline no demuestran exactly-once remoto, replay de proxy ni facturación. No atribuir este guard a los módulos legacy.

Si el precio caduca antes de un submit nuevo, no refrescar `$pricingPath` ni el manifest en el sitio. Conservar la corrida, preparar otra con evidencia vigente, calcular otra vez el peor caso y obtener autorización propia. Un request ID ya persistido puede seguir consultándose con el mismo ID aunque la evidencia haya envejecido.

## Evaluación secuencial

Cuando `execute` persiste el resultado raw:

```powershell
$runRecord = Get-Content -Raw -LiteralPath $runManifest | ConvertFrom-Json
$requestRecord = $runRecord.requests | Where-Object { $_.frame_index -eq $frameIndex }
$rawFramePath = Join-Path $runDir $requestRecord.result_path
& $python -B $pipeline evaluate `
    --run-manifest $runManifest `
    --frame-index $frameIndex `
    --raw $rawFramePath
if ($LASTEXITCODE -ne 0) { throw 'EVALUATION_FAILED' }
```

Leer el reporte real. Un PASS acepta técnicamente el frame y permite planificar el siguiente. Desde el frame 2, el plan referencia el master y el frame anterior aceptado. Un FAIL deja `TECHNICAL_REJECTED`: no adoptar ese frame como referencia, no pedir el siguiente y no ensamblar.

Repetir `next-request`, `execute` y `evaluate` de a un frame. No paralelizar solicitudes ni crear más de una animación/dirección en la corrida.

## Assembly

Con todos los frames aceptados y requests completados:

```powershell
& $python -B $pipeline assemble --run-manifest $runManifest
if ($LASTEXITCODE -ne 0) { throw 'ASSEMBLY_FAILED' }
```

Verificar el estado `READY_FOR_VISUAL_REVIEW`, los hashes y artefactos declarados. El sheet horizontal y los PNG RGBA son la referencia sin pérdida. El contact sheet y el GIF son vistas de revisión; el GIF usa 100 ms por frame. Revisar el enlace último-primero solamente si el movimiento solicitado debe repetirse.

`ASSEMBLY_RECOVERY_REQUIRED` o un `.rollback` retenido exige preservar todo y detener publicación automática. Recuperar comparando identidad, contenido y claims; no borrar ni repetir gasto para ocultar el estado.

## Revisión humana

El QA técnico no aprueba identidad, estilo ni movimiento artístico. Registrar el veredicto humano fuera de la corrida técnica, con los hashes exactos inspeccionados:

- `APPROVED`: aceptación visual explícita y sin correcciones abiertas;
- `APPROVED_WITH_CORRECTIONS`: conservar la lista concreta pendiente; no declarar entrega terminada;
- `REJECTED`: aceptación visual pendiente.

No sobrescribir `READY_FOR_VISUAL_REVIEW` para simular una aprobación.

## Stop, resume y correcciones

- Con ID conocido: conservar esquema, manifest, plan e ID; ejecutar `execute` sobre la misma corrida para consultar. No preparar, migrar ni enviar de nuevo.
- Sin ID después de submit: conservar `SUBMISSION_STATUS_UNKNOWN`, manifest y claim. Una ejecución diagnóstica puede confirmar el bloqueo, pero no autoriza otro POST.
- Con frame rechazado: conservar la corrida completa. No sobrescribir raw, frame, QA o reportes.
- Para corregir movimiento, prompt, fuente, precio caducado o una corrida sin ID, crear una corrida nueva. En un informe externo registrar `parent_run_id`, `child_run_id` y los hashes de ambos manifests; estos nombres no son campos del `run_manifest`.
- Calcular el nuevo máximo y obtener autorización explícita antes de cualquier solicitud de la corrida hija.
- No existe CLI de `repair`, `retry`, `submit`, `status` o `result`. `execute` contiene submit o reanudación según el estado durable.

## Comandos existentes

| Runtime | Comando | Función |
|---|---|---|
| Python | `prepare` | Crear una corrida v2 nueva en `DRAFT` |
| Python | `summary` | Mostrar el resumen completo de autorización |
| Python | `authorize` | Registrar el máximo exacto autorizado |
| Python | `next-request --output` | Escribir idempotentemente el próximo plan o devolver `null` |
| Node | `pricing --source --output` | Crear evidencia live autenticada o validar por separado un fixture offline marcado |
| Node | `execute --run-manifest --request-plan --pricing` | Enviar una solicitud nueva autorizada o reanudar el mismo ID |
| Python | `evaluate` | Normalizar y decidir un frame completado |
| Python | `assemble` | Ensamblar después de aceptar todos los frames |

No inventar flags de duración ni subcomandos del proveedor. `--client-module` existe únicamente para el harness falso bajo `NODE_ENV=test` y `FAKE_ROOT`; nunca habilitarlo para live.
