# Testes obrigatórios da API

Sempre que adicionares ou alterares uma funcionalidade da API:

1. Usa a skill api-testing instalada.
2. Cria ou atualiza testes automatizados para o comportamento
   novo ou alterado.
3. Reutiliza o framework do projeto. Em Python, prefere
   pytest com o cliente de testes do framework ou httpx.
4. Cobre os casos relevantes:
   - Pedidos válidos e respostas esperadas.
   - Dados inválidos e campos obrigatórios em falta.
   - Autenticação e permissões, quando aplicável.
   - Uploads válidos, inválidos, corrompidos e vazios,
     quando aplicável.
   - Estrutura das respostas e ficheiros devolvidos.
5. Executa os novos testes e os testes de regressão relevantes.
6. Corrige os problemas introduzidos pela alteração.
   Não enfraqueças testes para os fazer passar.
7. No fim, indica:
   - Testes que passaram, falharam ou não foram executados.
   - Problemas encontrados.
   - Comando para repetir os testes.

Executa testes que alteram dados apenas em ambiente local
ou de testes. Se não conseguires executar algum teste,
explica o impedimento e não declares que passou.
