$d = Split-Path $MyInvocation.MyCommand.Path; $u8 = New-Object Text.UTF8Encoding $false
$vp = 'width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover'
$head = Get-Content "$d\static\head.html" -Encoding UTF8; $i = ($head | Select-String -Pattern '^<svg width="0"' | Select-Object -First 1).LineNumber - 1
$pre = @('<!doctype html>','<html lang="sr">','<head>','<meta charset="utf-8">',"<meta name=`"viewport`" content=`"$vp`">",'<meta name="theme-color" content="#101214">','<link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png">','<link rel="icon" type="image/png" sizes="64x64" href="/static/favicon-64.png">','<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">','<link rel="manifest" href="/static/manifest.webmanifest">','<meta name="mobile-web-app-capable" content="yes">','<meta name="apple-mobile-web-app-capable" content="yes">','<meta name="apple-mobile-web-app-title" content="Teco.Pi">','<meta name="apple-mobile-web-app-status-bar-style" content="black">','<style>[hidden]{display:none!important}body{margin:0}img{max-width:100%}</style>') + (Get-Content "$d\static\splash\links.html" -Encoding UTF8)
$out = $pre + $head[0..($i-1)] + @('</head>','<body>') + ((Get-Content "$d\static\boot.html" -Encoding UTF8) -replace '\{\{BOOT_BG\}\}', [Convert]::ToBase64String([IO.File]::ReadAllBytes("$d\static\boot-bg.png"))) + $head[$i..($head.Count-1)] + (Get-Content "$d\static\tail.html" -Encoding UTF8)
[IO.File]::WriteAllLines("$d\static\index.html", $out, $u8)
$html = [IO.File]::ReadAllText("$d\static\index.html"); [IO.File]::WriteAllText("$d\check.js", (([regex]::Matches($html, '(?s)<script>(.*?)</script>') | ForEach-Object { $_.Groups[1].Value }) -join ";`n"))
& node --check "$d\check.js"; if (-not $?) { exit 1 }
# $('#id') u JS-u bez elementa sa tim id-jem obori render() na svakom stanju (stranica „dugo učitava“, ništa se ne osvežava)
$ids = [Collections.Generic.HashSet[string]]::new([string[]]@([regex]::Matches($html, '\sid="([^"]+)"') | ForEach-Object { $_.Groups[1].Value }))
$js = [IO.File]::ReadAllText("$d\check.js")
[regex]::Matches($js, "\.id = '([\w-]+)'") | ForEach-Object { [void]$ids.Add($_.Groups[1].Value) }   # elementi koje pravi JS
$miss = [regex]::Matches($js, "\`$\('#([\w-]+)'\)") | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique | Where-Object { -not $ids.Contains($_) }
if ($miss) { Write-Host "Nema elementa za: $($miss -join ', ')" -ForegroundColor Red; exit 1 }