// Font Awesome Free SVG icons, vendored as a small offline sprite (CC BY 4.0).
const fs = require('node:fs');
const path = require('node:path');
const icons = require('@fortawesome/free-solid-svg-icons');
const selected = {
  satellite: icons.faSatellite, cloud: icons.faCloud, chip: icons.faMicrochip,
  database: icons.faDatabase, upload: icons.faCloudArrowUp, published: icons.faCircleCheck,
  map: icons.faMap, chart: icons.faChartLine, clock: icons.faClock,
  viewer: icons.faMobileScreenButton, arrow: icons.faArrowRight, refresh: icons.faRotate,
  code: icons.faCode, sun: icons.faSun, layers: icons.faLayerGroup,
};
const symbols = Object.entries(selected).map(([name, icon]) => {
  const [width, height, , , data] = icon.icon;
  const paths = (Array.isArray(data) ? data : [data]).map(d => `<path d="${d}"/>`).join('');
  return `<symbol id="${name}" viewBox="0 0 ${width} ${height}">${paths}</symbol>`;
});
const out = path.join(__dirname, 'web', 'img');
fs.writeFileSync(path.join(out, 'fontawesome.svg'),
  '<!-- Font Awesome Free 6.7.2 · https://fontawesome.com · Icons licensed CC BY 4.0 -->\n' +
  `<svg xmlns="http://www.w3.org/2000/svg">\n${symbols.join('\n')}\n</svg>\n`);
fs.copyFileSync(path.join(path.dirname(require.resolve('@fortawesome/free-solid-svg-icons/package.json')), 'LICENSE.txt'), path.join(out, 'fontawesome-LICENSE.txt'));
