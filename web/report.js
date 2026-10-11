// кнопка «Печать / PDF» в отчётах (встроенные скрипты запрещены политикой безопасности страницы)
document.addEventListener('DOMContentLoaded', function () {
  var b = document.getElementById('print');
  if (b) b.addEventListener('click', function () {
    document.querySelectorAll('details').forEach(function (d) { d.open = true; });
    window.print();
  });
});
