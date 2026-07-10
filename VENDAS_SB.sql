SELECT 
    unidadenegocio.nome AS loja,
    us_orc.apelido AS usuario_orcamento,
    venda.id AS vendaid,
    venda.status AS status_venda,
    embalagem.descricao,
    embalagem.etiqueta, 
    embalagem.codigobarras,
    produto.codigo as cod_interno,
    CASE	
    	WHEN classificacao.caminho LIKE '%>%-%' THEN SPLIT_PART(classificacao.caminho, '>',2)
    END AS classificacaoo,
    itemvenda.quantidade,
    itemvenda.valorunitario,
    itemorcamento.precovenda,
    me.custo AS custo,
    me.quantidade AS qtd_mov,
    itemvenda.desconto,
    itemvenda.valortotal,
    (itemvenda.valortotal - me.custo*itemvenda.quantidade) AS lucro,
    (itemvenda.valortotal - me.custo*itemvenda.quantidade)/NULLIF(itemvenda.valortotal,0) AS lucro_perc,
    venda.datahorafechamento AS datahora_venda_final,
    me.datahora,
    venda.cpfcnpj,
    itemvenda.status AS status_item_venda,
    pbm_primario.nome AS programa_pbm,
    pbm_secundario.nome AS programa_pbm_secundario,
    CASE
    	WHEN 'PEC - FIDELIDADE' IN (pbm_primario.nome, pbm_secundario.nome)
        THEN 'PEC'
    ELSE 'NAO-PEC'
	END AS PEC,
	venda.coo,
    SUM(itemvenda.quantidade) OVER(
        PARTITION BY itemvenda.embalagemid, itemvenda.unidadenegocioid
        ORDER BY me.datahora
        RANGE BETWEEN INTERVAL '90 DAYS' PRECEDING AND CURRENT ROW
    ) / 90.0 AS media_venda_dia,
    SUM(itemvenda.quantidade) OVER(
        PARTITION BY itemvenda.embalagemid, itemvenda.unidadenegocioid
        ORDER BY me.datahora
        RANGE BETWEEN INTERVAL '90 DAYS' PRECEDING AND CURRENT ROW
    ) / 3.0 AS media_venda_mes
FROM itemvenda 
JOIN venda ON venda.id = itemvenda.vendaid
	AND venda.status = 'F' 
JOIN embalagem ON itemvenda.embalagemid = embalagem.id
LEFT JOIN produto ON embalagem.produtoid = produto.id
LEFT JOIN classificacaoproduto ON produto.id = classificacaoproduto.produtoid
LEFT JOIN classificacao ON classificacaoproduto.classificacaoid  = classificacao.id
	AND classificacao.principal = true
LEFT JOIN itemorcamento ON itemvenda.itemorcamentoid  = itemorcamento.id
LEFT JOIN orcamento ON itemorcamento.orcamentoid = orcamento.id
LEFT JOIN usuario us_orc ON us_orc.id = itemorcamento.usuarioid
LEFT JOIN usuario us_ven ON us_ven.id = venda.usuarioid
LEFT JOIN unidadenegocio ON unidadenegocio.id = itemvenda.unidadenegocioid
LEFT JOIN orcamentopbm ON orcamento.id = orcamentopbm.orcamentoid
LEFT JOIN pbm pbm_primario ON orcamentopbm.pbmid = pbm_primario.id
LEFT JOIN pbm pbm_secundario ON pbm_secundario.id = orcamentopbm.pbmsecundariaid
LEFT JOIN movimentacaoestoque me ON itemvenda.movimentacaoestoqueid = me.id
    AND itemvenda.datahora  = me.datahora
	AND me.unidadenegocioid = itemvenda.unidadenegocioid
WHERE me.tipomovimentacaoestoqueid = 26
  AND itemvenda.status = 'F' 
  AND me.datahora BETWEEN '2026-02-01 00:00:00' AND '2026-05-28 23:59:59' 
  AND classificacao.caminho NOT LIKE ('%USO CONSUMO%')
