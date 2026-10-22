// Callsign prefix -> country and an approximate position, for stations that never sent a grid.
// Longest prefix wins. Positions are country (or call-area) centroids: good to a few hundred km at best.
'use strict';

const PREFIX_TABLE = `
K W N AA AB AC AD AE AF AG AI AJ AK|United States|39|-98
KH6 NH6 WH6 AH6 KH7|Hawaii|20.8|-156.3
KL7 NL7 WL7 AL7 KL AL|Alaska|64|-150
KP4 NP4 WP4 KP3|Puerto Rico|18.2|-66.5
KP2 NP2 WP2|US Virgin Islands|18.3|-64.9
KH2 NH2 WH2 AH2|Guam|13.4|144.8
KH0 NH0|Mariana Islands|15.2|145.7
VE VA VO VY CY CZ XL XM|Canada|56|-106
XE XF 4A 4B 6D 6E 6F 6G 6H 6I 6J|Mexico|23.6|-102.5
CO CM CL T4|Cuba|21.5|-79.5
HI|Dominican Republic|18.7|-70.2
HH|Haiti|19|-72.5
6Y|Jamaica|18.1|-77.3
C6|Bahamas|24.5|-77.5
TI TE|Costa Rica|9.9|-84
HP HO|Panama|8.5|-80
TG|Guatemala|15.5|-90.3
HR HQ|Honduras|15|-86.5
YS|El Salvador|13.8|-88.9
YN|Nicaragua|12.9|-85.2
V3|Belize|17.2|-88.5
HK HJ 5K 5J|Colombia|4.6|-74
YV YY 4M|Venezuela|7|-66
HC HD|Ecuador|-1.8|-78
HC8|Galapagos|-0.7|-90.3
OA OB OC 4T|Peru|-9.2|-75
PY PP PQ PR PS PT PU PV PW PX ZV ZW ZX ZY ZZ|Brazil|-14|-51
LU LW LO LP LQ LR LS LT LV AY AZ L2 L3 L4 L5 L6 L7 L8 L9|Argentina|-38|-63.6
CE CA CB CC CD XQ XR 3G|Chile|-35|-71
CE0Y|Easter Island|-27.1|-109.4
CX CV CW|Uruguay|-32.5|-55.8
ZP|Paraguay|-23.4|-58.4
CP|Bolivia|-16.3|-63.6
9Y 9Z|Trinidad and Tobago|10.4|-61.3
8P|Barbados|13.2|-59.5
P4|Aruba|12.5|-70
PJ2|Curacao|12.2|-69
PJ4|Bonaire|12.2|-68.3
V4|St Kitts and Nevis|17.3|-62.7
V2|Antigua|17.1|-61.8
J3|Grenada|12.1|-61.7
J6|St Lucia|13.9|-61
J7|Dominica|15.4|-61.4
J8|St Vincent|13.2|-61.2
FM|Martinique|14.6|-61
FG|Guadeloupe|16.2|-61.6
ZF|Cayman Islands|19.3|-81.3
VP9|Bermuda|32.3|-64.8
VP2E|Anguilla|18.2|-63.1
VP2V|British Virgin Islands|18.4|-64.6
VP5|Turks and Caicos|21.7|-71.8
8R|Guyana|4.9|-58.9
PZ|Suriname|4|-56
FY|French Guiana|4|-53
OX XP|Greenland|72|-40
TF|Iceland|64.9|-18
G M 2E 2D 2I 2M 2W 2U 2J|England|52.5|-1.5
GM MM 2M|Scotland|57|-4
GW MW 2W|Wales|52.3|-3.7
GI MI 2I|Northern Ireland|54.6|-6.7
GD MD 2D|Isle of Man|54.2|-4.5
GU MU 2U|Guernsey|49.45|-2.6
GJ MJ 2J|Jersey|49.2|-2.1
EI EJ|Ireland|53.4|-8
F TM|France|46.6|2.4
DA DB DC DD DE DF DG DH DI DJ DK DL DM DN DO DP DQ DR|Germany|51.2|10.4
PA PB PC PD PE PF PG PH PI|Netherlands|52.1|5.3
ON OO OP OQ OR OS OT|Belgium|50.5|4.5
LX|Luxembourg|49.8|6.1
EA EB EC ED EE EF EG EH|Spain|40.4|-3.7
EA6 EB6 EC6 ED6 EE6 EF6 EG6 EH6|Balearic Islands|39.6|2.9
EA8 EB8 EC8 ED8 EE8 EF8 EG8 EH8|Canary Islands|28.3|-15.8
CT CQ CR CS|Portugal|39.5|-8
CT3 CQ3 CR3|Madeira|32.7|-17
CU|Azores|38.5|-28
I IA IB IC ID IE IF IG IH II IJ IK IL IM IN IO IP IQ IR IS IT IU IV IW IX IY IZ|Italy|42.8|12.5
IS0 IM0|Sardinia|40.1|9
IT9|Sicily|37.6|14
HB|Switzerland|46.8|8.2
HB0|Liechtenstein|47.2|9.55
OE|Austria|47.5|14.5
OK OL|Czech Republic|49.8|15.5
OM|Slovakia|48.7|19.7
SP SQ SO SN SR 3Z HF|Poland|52|19
HA HG|Hungary|47.2|19.5
S5|Slovenia|46.1|14.9
9A|Croatia|45.1|15.2
E7|Bosnia and Herzegovina|44|17.7
YT YU|Serbia|44|21
4O|Montenegro|42.7|19.3
Z3|North Macedonia|41.6|21.7
ZA|Albania|41.2|20
SV SX SY SZ J4|Greece|39|22
SV9|Crete|35.2|24.9
LZ|Bulgaria|42.7|25.5
YO YP YQ YR|Romania|45.9|25
ER|Moldova|47.4|28.4
UR US UT UU UV UW UX UY UZ EM EN EO|Ukraine|49|32
EU EV EW|Belarus|53.7|28
LY|Lithuania|55.2|23.9
YL|Latvia|56.9|24.6
ES|Estonia|58.6|25
OH OF OG OI|Finland|61.9|25.7
OH0|Aland Islands|60.2|20
SM SA SB SC SD SE SF SG SH SI SJ SK SL 7S 8S|Sweden|60.1|18.6
LA LB LC LD LE LF LG LH LI LJ LK LL LM LN|Norway|60.5|8.5
JW|Svalbard|78.2|15.6
OZ OU OV 5P 5Q|Denmark|56.3|9.5
OY|Faroe Islands|62|-7
R RA RB RC RD RE RF RG RJ RK RL RM RN RO RQ RT RU RV RW RX RZ UA UB UC UD UE UF UG UH UI|Russia|55.7|37.6
UN UO UP UQ|Kazakhstan|48|67
4L|Georgia|42.3|43.4
EK|Armenia|40|45
4J 4K|Azerbaijan|40.1|47.6
EX|Kyrgyzstan|41.2|74.8
EY|Tajikistan|38.9|71.3
EZ|Turkmenistan|38.9|59.6
UJ UK UL UM|Uzbekistan|41.4|64.6
TA TB TC YM|Turkey|39|35
5B C4 H2 P3|Cyprus|35|33
4X 4Z|Israel|31.5|34.9
OD|Lebanon|33.9|35.9
JY|Jordan|31|36.5
HZ 7Z 8Z|Saudi Arabia|23.9|45
A6|United Arab Emirates|24|54
A7|Qatar|25.3|51.2
A9|Bahrain|26|50.5
9K|Kuwait|29.3|47.5
A4|Oman|21.5|55.9
EP EQ|Iran|32.4|53.7
YI|Iraq|33.2|43.7
9H|Malta|35.9|14.4
3A|Monaco|43.7|7.4
C3|Andorra|42.5|1.5
T7|San Marino|43.9|12.45
ZB|Gibraltar|36.1|-5.35
JA JE JF JG JH JI JJ JK JL JM JN JO JP JQ JR JS 7J 7K 7L 7M 7N 8J 8K 8L 8M 8N|Japan|36.2|138.3
HL DS DT 6K 6L 6M 6N|South Korea|36.5|127.8
BA BB BC BD BE BF BG BH BI BJ BK BL BY BZ|China|35|103
BM BN BO BP BQ BU BV BW BX|Taiwan|23.7|121
VR2|Hong Kong|22.3|114.2
XX9|Macau|22.2|113.55
JT JU JV|Mongolia|46.9|103.8
DU DV DW DX DY DZ 4D 4E 4F 4G 4H 4I|Philippines|12.9|121.8
XV 3W|Vietnam|14|108.3
HS E2|Thailand|15.9|100.9
9M2 9M4|West Malaysia|4.2|101.9
9M6 9M8|East Malaysia|3|113
YB YC YD YE YF YG YH 7A 7B 7C 7D 7E 7F 7G 7H 7I 8A 8B 8C 8D 8E 8F 8G 8H 8I|Indonesia|-2.5|118
9V|Singapore|1.35|103.8
V8|Brunei|4.5|114.7
VU AT AU AV AW 8T 8U 8V 8W 8X 8Y|India|21|78
4S|Sri Lanka|7.9|80.8
AP AQ AR AS 6P 6Q 6R 6S|Pakistan|30.4|69.3
S2 S3|Bangladesh|23.7|90.4
9N|Nepal|28.4|84.1
XW|Laos|19.9|102.5
XU|Cambodia|12.6|104.9
XZ|Myanmar|21.9|95.9
VK AX|Australia|-25|134
ZL ZM|New Zealand|-41|174
3D2|Fiji|-17.8|178
5W|Samoa|-13.8|-172
A3|Tonga|-21.2|-175.2
E5|Cook Islands|-21.2|-159.8
FO|French Polynesia|-17.7|-149.4
FK|New Caledonia|-21|165.6
P2|Papua New Guinea|-6.3|143.9
H4|Solomon Islands|-9.6|160
YJ|Vanuatu|-15.4|166.9
T3|Kiribati|1.9|-157.4
V7|Marshall Islands|7.1|171.2
V6|Micronesia|6.9|158.2
T8|Palau|7.5|134.6
ZS ZR ZT ZU|South Africa|-30.6|22.9
V5|Namibia|-22.6|17
A2|Botswana|-22.3|24.7
Z2|Zimbabwe|-19|29.2
9J|Zambia|-13.1|27.8
5Y 5Z|Kenya|0|37.9
5H|Tanzania|-6.4|34.9
5X|Uganda|1.4|32.3
ET|Ethiopia|9.1|40.5
SU|Egypt|26.8|30.8
CN 5C 5D 5E 5F 5G|Morocco|31.8|-7.1
7X|Algeria|28|1.7
3V|Tunisia|34|9.5
5A|Libya|26.3|17.2
5N|Nigeria|9.1|8.7
9G|Ghana|7.9|-1
6W|Senegal|14.5|-14.5
D4|Cape Verde|16|-24
5R|Madagascar|-18.8|46.9
3B8|Mauritius|-20.3|57.6
FR|Reunion|-21.1|55.5
7P|Lesotho|-29.6|28.2
3DA|Eswatini|-26.5|31.5
C9|Mozambique|-18.7|35.5
D2|Angola|-11.2|17.9
TJ|Cameroon|7.4|12.4
TU|Ivory Coast|7.5|-5.5
TZ|Mali|17.6|-4
9Q|DR Congo|-4|21.8
9X|Rwanda|-1.9|29.9
J2|Djibouti|11.8|42.6
ST|Sudan|12.9|30.2
`;

// Call-area digit -> regional centroid, for big countries
const AREAS = {
  'United States': {1: [42.5, -71.5], 2: [41.5, -74.5], 3: [40, -77], 4: [33, -84], 5: [32, -96],
                    6: [36.5, -119.5], 7: [43, -115], 8: [40.5, -83], 9: [41.5, -88], 0: [41, -97]},
  Canada: {1: [45, -63], 2: [47, -71.5], 3: [44.5, -79.5], 4: [50, -97.5], 5: [52, -106], 6: [53, -114],
           7: [49.5, -123], 8: [62.5, -114], 9: [46, -66]},
  Australia: {1: [-35.3, 149.1], 2: [-32, 147], 3: [-37, 144.5], 4: [-22, 144], 5: [-30, 135.5],
              6: [-26, 121], 7: [-42, 146.5], 8: [-19.5, 133.5]},
  Russia: {1: [59.9, 30.3], 2: [54.7, 20.5], 3: [55.7, 37.6], 4: [53, 48], 5: [55.7, 37.6], 6: [45, 40],
           7: [55.7, 37.6], 8: [56, 90], 9: [55, 73], 0: [60, 110]},
};

const PREFIXES = new Map();
for (const line of PREFIX_TABLE.trim().split('\n')) {
  const [codes, name, lat, lon] = line.split('|');
  for (const p of codes.split(' ')) PREFIXES.set(p, {name, lat: +lat, lon: +lon});
}
const MAX_PREFIX = Math.max(...[...PREFIXES.keys()].map((p) => p.length));

// The part of a call that says where it is: "P4/WE9V" -> "P4", "W1AW/7" -> "W1AW", "VE3XYZ" -> itself
function homeCall(call) {
  const parts = call.split('/');
  if (parts.length === 1) return call;
  const [a, b] = parts;
  if (a.length < b.length && a.length <= 4) return a;
  return a.length >= b.length ? a : b;
}

function prefixInfo(call) {
  if (!call || call.startsWith('<')) return null;
  const c = homeCall(call.toUpperCase());
  for (let n = Math.min(MAX_PREFIX, c.length); n >= 1; n--) {
    const hit = PREFIXES.get(c.slice(0, n));
    if (!hit) continue;
    const areas = AREAS[hit.name];
    const digit = (c.slice(n - 1).match(/\d/) || c.match(/\d/) || [])[0];
    if (areas && digit != null && areas[digit]) return {name: hit.name, lat: areas[digit][0], lon: areas[digit][1], area: true};
    return hit;
  }
  return null;
}
