# AirPrint Bridge

Rendez n'importe quelle imprimante réseau visible en **AirPrint** depuis vos Mac, iPhone et iPad — même si elle ne le supporte pas nativement.

Le conteneur embarque **CUPS** (pilotage de l'imprimante), **Avahi** (annonce Bonjour/mDNS) et la **base de drivers OpenPrinting** (Gutenprint, HPLIP, brlaser, SpliX, foomatic…), le tout piloté par une interface web minimaliste :

1. entrez l'adresse IP de l'imprimante ;
2. le modèle est détecté automatiquement (SNMP/IPP) et le driver recommandé est pré-sélectionné — recherche manuelle ou fichier PPD en repli ;
3. un clic, et l'imprimante apparaît sur tous vos appareils Apple.

## Installation sur CasaOS

1. Dans CasaOS, ouvrez l'App Store → **Install a customized app** (icône `+`).
2. Renseignez :
   - **Image** : `ghcr.io/moifort/airprint:latest`
   - **Network** : `host` *(obligatoire — voir plus bas)*
   - **Volume** : `/DATA/AppData/airprint/cups` → `/etc/cups`
   - **Variable d'environnement** (optionnel) : `UI_PORT=8080`
3. Installez, puis ouvrez `http://<ip-du-serveur>:8080`.

Ou avec docker compose, depuis ce dépôt :

```bash
docker compose up -d
```

## Configuration

| Variable | Défaut | Rôle |
|----------|--------|------|
| `UI_PORT` | `8080` | Port de l'interface web |

Le port `631` (CUPS/IPP) est également exposé : l'administration CUPS classique reste accessible sur `http://<ip-du-serveur>:631` si besoin.

## Pourquoi le mode réseau `host` ?

AirPrint repose sur le **mDNS** (multicast DNS, port 5353) : les appareils Apple découvrent les imprimantes en écoutant les annonces Bonjour sur le réseau local. Le réseau *bridge* de Docker ne laisse pas passer ce trafic multicast — sans `network_mode: host`, l'imprimante ne sera jamais visible.

## Dépannage

- **L'imprimante n'apparaît pas sur le Mac** : vérifiez le mode réseau `host`, puis sur un Mac lancez `dns-sd -B _ipp._tcp` — l'imprimante doit être listée. Vérifiez aussi que le serveur et le Mac sont sur le même réseau/VLAN.
- **Conflit Avahi** : si l'hôte fait déjà tourner `avahi-daemon` (port 5353 occupé), le conteneur ne pourra pas annoncer les imprimantes. Désactivez l'avahi de l'hôte (`systemctl disable --now avahi-daemon`) ou utilisez-le pour publier les services.
- **Modèle non détecté** : certaines imprimantes n'exposent ni SNMP ni IPP. Utilisez la recherche manuelle de driver (base OpenPrinting embarquée) ou fournissez le fichier PPD du constructeur.
- **L'impression échoue malgré la détection** : essayez une autre connexion dans le sélecteur (`socket://` fonctionne sur la plupart des imprimantes, port 9100).

## Développement

```bash
pip install -r requirements-dev.txt
pytest

docker build -t airprint .
docker run --rm --network host airprint
```

À chaque push sur `main` (et tag `v*`), l'image multi-architecture (amd64 + arm64) est publiée sur GHCR par GitHub Actions.
