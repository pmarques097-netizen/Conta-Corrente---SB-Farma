/*
  NEGOCIACAO_ENTRADAS_OTIMIZADA.sql - v22
  Uso: Projeto Negociação - apuração de investimento sobre compra.

  Regras:
  - Fabricante = laboratorio
  - Distribuidor/Fornecedor = fornecedor
  - Compra Base = coluna Compra do sistema, calculada por custo final do item: inf.custo / quantidade por embalagem * quantidade
  - Data da apuração = DATA DE EMISSÃO DA NOTA (nf.datahoraemissao)
  - Data de entrada fica disponível apenas para conferência
  - Período padrão de carga: últimos 180 dias pela emissão da nota
*/
SELECT
    nf.datahoraemissao::date AS data_emissao,
    nf.datahoraentrada::date AS data_entrada,
    uni.codigo AS numero_loja,
    nf.numero AS numero_nf,
    inf.codigobarras AS codigobarras,
    emb.descricao AS descricao_embalagem,
    COALESCE(pefab.razaosocial, '') AS laboratorio,
    COALESCE(pes.nome, '') AS fornecedor,
    inf.quantidade::numeric(18,4) AS quantidade_por_produto,
    COALESCE(emb.quantidadeporembalagem, 1)::numeric(18,4) AS quant_embalagem,
    inf.valorunitario::numeric(18,4) AS valor_nf_unitario,
    inf.custo::numeric(18,4) AS custo_nf_unitario,
    (COALESCE(inf.custo, 0) / NULLIF(COALESCE(emb.quantidadeporembalagem, 1), 0))::numeric(18,4) AS valor_unitario_compra_sistema,
    ((COALESCE(inf.custo, 0) / NULLIF(COALESCE(emb.quantidadeporembalagem, 1), 0)) * COALESCE(inf.quantidade, 0))::numeric(18,2) AS valor_entrada,
    ((COALESCE(inf.custo, 0) / NULLIF(COALESCE(emb.quantidadeporembalagem, 1), 0)) * COALESCE(inf.quantidade, 0))::numeric(18,2) AS valor_nf_total,
    inf.cfop AS cfop
FROM notafiscal AS nf
JOIN itemnotafiscal AS inf
    ON inf.notafiscalid = nf.id
LEFT JOIN unidadenegocio AS uni
    ON uni.id = nf.unidadenegocioid
LEFT JOIN fornecedor AS forn
    ON forn.id = nf.fornecedorid
LEFT JOIN pessoa AS pes
    ON pes.id = forn.pessoaid
LEFT JOIN embalagem AS emb
    ON emb.id = inf.embalagemid
LEFT JOIN produto AS prod
    ON prod.id = emb.produtoid
LEFT JOIN fabricante AS fab
    ON fab.id = prod.fabricanteid
LEFT JOIN pessoa AS pefab
    ON pefab.id = fab.pessoaid
WHERE nf.status = 'C'
  AND nf.datahoraemissao::date >= CURRENT_DATE - INTERVAL '180 days'
  AND nf.datahoraemissao::date < CURRENT_DATE + INTERVAL '1 day'
  AND uni.codigo NOT IN ('14-2', '24-2', '26', '40', '41', 'BKP', 'CLOUD', 'ESC')
  AND COALESCE(inf.custo, 0) > 0
  AND COALESCE(inf.quantidade, 0) > 0;
