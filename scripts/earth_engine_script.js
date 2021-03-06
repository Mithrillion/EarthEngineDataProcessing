 
var area = ee.FeatureCollection(testArea);
var coords = testPoint.coordinates().getInfo();  // use getInfo() to retrieve value
Map.setCenter(coords[0], coords[1], 12);

var collection = ee.ImageCollection('LANDSAT/LC8_SR')
    .filterDate('2015-03-01', '2016-02-01')
    .filterBounds(area);
    // .map(createTimeBand);
    
print(collection);

var vizParams = {
  min: 200,
  max: 2000,
  bands: ['B5', 'B4', 'B3']
};

function cropCollection(collection) {
  return collection.clipToCollection(area);
}

Map.addLayer(collection.map(cropCollection).median(), vizParams);

var _collection = collection.getInfo();

Export.table.toDrive({
  collection: collection,
  description: _collection.id.split('/')[1],
  folder: 'earthengine_tables',
});

var imgs = _collection.features.map(function(img){
  return ee.Image.load(img.id);
});

imgs.forEach(function(img){
  Export.image.toDrive({
    image: img.select(['B5', 'B4', 'B3']),
    description: img.id().getInfo(),
    folder: 'earthengine_images',
    scale: 30,
    region: area,
    crs: 'EPSG:3857'
  });
  Export.image.toDrive({
    image: img.select(['cfmask', 'cfmask_conf']),
    description: img.id().getInfo(),
    folder: 'earthengine_masks',
    scale: 30,
    region: area,
    crs: 'EPSG:3857'
  });
});
